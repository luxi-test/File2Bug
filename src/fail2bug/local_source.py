"""本地 Excel / ZIP 输入，附件只从选定目录或压缩包内读取。"""
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
import mimetypes
import posixpath
import re
import shutil
import stat
import zipfile
import os
from xml.etree import ElementTree

from openpyxl import load_workbook

from .parser import InputError, parse_table, HEADERS, column_header, clean, cell_text

MEDIA_EXTENSIONS = {'.png','.jpg','.jpeg','.gif','.webp','.bmp','.heic','.heif','.mp4','.mov','.m4v','.webm','.mkv','.avi','.txt','.log','.pdf'}
DISPIMG = re.compile(r'=\s*(?:_xlfn\.)?DISPIMG\s*\(\s*"([^"]+)"\s*[,;]\s*\d+\s*\)', re.IGNORECASE)


def _validate_workbook_size(path: Path, max_bytes: int):
    try:
        with zipfile.ZipFile(path) as workbook:
            if sum(entry.file_size for entry in workbook.infolist()) > max_bytes:
                raise InputError('Excel 展开后超过导入大小上限。')
    except zipfile.BadZipFile as exc:
        raise InputError('Excel 文件损坏。') from exc


def safe_extract(archive: Path, root: Path, max_bytes: int):
    try:
        with zipfile.ZipFile(archive) as bundle:
            entries = bundle.infolist()
            if len(entries) > 500 or sum(entry.file_size for entry in entries) > max_bytes:
                raise InputError('压缩包文件过多或解压后超过导入大小上限。')
            for entry in entries:
                name = entry.filename.replace('\\','/')
                parts = PurePosixPath(name)
                if not parts.parts or parts.is_absolute() or '..' in parts.parts or ':' in parts.parts[0] or stat.S_ISLNK(entry.external_attr >> 16):
                    raise InputError('压缩包包含不安全的路径，未导入。')
                target = root.joinpath(*parts.parts)
                if not target.resolve().is_relative_to(root.resolve()):
                    raise InputError('压缩包路径超出导入目录。')
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(entry) as source, target.open('xb') as output:
                        shutil.copyfileobj(source, output)
    except (zipfile.BadZipFile, OSError) as exc:
        raise InputError('压缩包损坏或存在重复文件，请重新打包。') from exc


@contextmanager
def open_local(path: Path, *, max_bytes: int):
    path = path.expanduser().resolve()
    if not path.is_file() or path.suffix.lower() not in {'.xlsx','.zip'}:
        raise InputError('请选择 .xlsx 执行表，或包含一个 .xlsx 和证据文件的 .zip。')
    if path.stat().st_size > max_bytes:
        raise InputError('文件超过本地导入大小上限。')
    if path.suffix.lower() == '.zip':
        with TemporaryDirectory(prefix='f2b-import-') as temp:
            root = Path(temp)
            safe_extract(path, root, max_bytes)
            books = [book for book in root.rglob('*') if book.suffix.lower()=='.xlsx' and not book.name.startswith('~$') and '__MACOSX' not in book.parts]
            if len(books) != 1:
                raise InputError('ZIP 中需要恰好一个 .xlsx 执行表。')
            _validate_workbook_size(books[0], max_bytes)
            yield books[0], root
    else:
        _validate_workbook_size(path, max_bytes)
        yield path, path.parent


def _cell_image_targets(archive: zipfile.ZipFile) -> dict[str, str]:
    try:
        pictures = ElementTree.fromstring(archive.read('xl/cellimages.xml'))
        relationships = ElementTree.fromstring(archive.read('xl/_rels/cellimages.xml.rels'))
    except (KeyError, ElementTree.ParseError):
        return {}
    namespace = {
        'xdr': 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing',
        'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
    }
    targets = {}
    for relation in relationships:
        if relation.get('TargetMode') == 'External' or not relation.get('Type', '').endswith('/image'):
            continue
        target = relation.get('Target', '')
        member = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join('xl', target))
        content_type = mimetypes.guess_type(member)[0] or ''
        if member.startswith('xl/media/') and content_type.startswith('image/') and content_type != 'image/svg+xml':
            targets[relation.get('Id')] = member
    images = {}
    for picture in pictures.findall('.//xdr:pic', namespace):
        properties = picture.find('xdr:nvPicPr/xdr:cNvPr', namespace)
        blip = picture.find('xdr:blipFill/a:blip', namespace)
        if properties is not None and blip is not None:
            relationship = blip.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed')
            if properties.get('name') and relationship in targets:
                images[properties.get('name')] = targets[relationship]
    return images


def _attach_cell_images(path, sheet_name, evidence_column, by_row, attachments, warnings):
    with zipfile.ZipFile(path) as archive:
        targets = _cell_image_targets(archive)
        references = {row: DISPIMG.fullmatch(item.get('evidence', '')) for row, item in by_row.items()}
        if 'xl/cellimages.xml' in archive.namelist():
            # 某些文件没有写入 DISPIMG 的缓存值，读取公式文本即可定位图片。
            formulas = load_workbook(path, data_only=False, read_only=True, keep_links=False)
            try:
                cells = formulas[sheet_name].iter_rows(min_row=min(by_row), max_row=max(by_row),
                    min_col=evidence_column, max_col=evidence_column, values_only=True)
                for row, (value,) in enumerate(cells, start=min(by_row)):
                    if row in references and not references[row] and isinstance(value, str):
                        references[row] = DISPIMG.fullmatch(value)
            finally:
                formulas.close()
        image_data = {}
        members = set(archive.namelist())
        for row, reference in references.items():
            if not reference:
                continue
            item = by_row[row]
            if DISPIMG.fullmatch(item.get('evidence', '')):
                item['evidence'] = ''
            member = targets.get(reference[1])
            if member not in members:
                warnings.append(f"{item['case_id']}：证据单元格的内嵌图片无法读取")
                continue
            if member not in image_data:
                image_data[member] = archive.read(member)
            extension = PurePosixPath(member).suffix.lower()
            attachments[item['id']].append({
                'name': f"{item['case_id']}-内嵌证据{extension}",
                'data': image_data[member],
                'content_type': mimetypes.guess_type(member)[0],
            })


def read_workbook(path: Path, root: Path, *, sheet: str | None, max_rows: int, max_columns: int):
    try:
        workbook = load_workbook(path, data_only=True, keep_links=False)
    except Exception as exc:
        raise InputError('无法读取 Excel，请检查是否为未加密的 .xlsx 文件。') from exc
    try:
        if sheet and sheet not in workbook.sheetnames:
            raise InputError('没有找到工作表：' + sheet)
        page = workbook[sheet] if sheet else workbook.worksheets[0]
        if page.max_row > max_rows or page.max_column > max_columns:
            raise InputError('Excel 工作表范围超过行列上限，请复制所需区域到单独工作表。')
        rows = [[cell.value for cell in row] for row in page.iter_rows()]
        evidence_column = None
        if rows:
            header_at=max(range(min(10,len(rows))),key=lambda index:sum(column_header(value) in HEADERS for value in rows[index]))
            for column,value in enumerate(rows[header_at]):
                if clean(cell_text(value))=='证据':
                    evidence_column = column + 1
                    for index in range(header_at+1,len(rows)):
                        cell=page.cell(index+1,column+1)
                        if cell.hyperlink and cell.hyperlink.target:
                            rows[index][column]=cell.hyperlink.target
        parsed = parse_table(rows, '')
        parsed['source_label'] = f'local:{path.name}#{page.title}'
        parsed['warnings'] = []
        attachments = {item['id']: [] for item in parsed['items']}
        by_row = {item['row_no']: item for item in parsed['items']}
        if evidence_column and by_row:
            _attach_cell_images(path, page.title, evidence_column, by_row, attachments, parsed['warnings'])
        # 标准 Excel 绘图图片按锚点所在用例行关联，不把封面或表头图片当成证据。
        for index, image in enumerate(getattr(page, '_images', []), 1):
            anchor = getattr(image.anchor, '_from', None)
            item = by_row.get(anchor.row + 1) if anchor is not None else None
            if item:
                name = f"{item['case_id']}-内嵌图片-{index}.{image.format}"
                attachments[item['id']].append({'name': name, 'data': image._data(), 'content_type': mimetypes.guess_type(name)[0] or 'image/png'})
        by_name = {}
        scanned = 0
        if any(item.get('evidence') for item in parsed['items']):
            for folder, directories, names in os.walk(root):
                directories[:] = [name for name in directories if not name.startswith('.') and name not in {'node_modules','__MACOSX'}]
                scanned += len(names)
                if scanned > 5000:
                    parsed['warnings'].append('附件目录文件过多，未继续递归查找；建议将用到的附件和 Excel 单独打包。')
                    break
                for name in names:
                    candidate = Path(folder) / name
                    if candidate.is_file() and candidate.suffix.lower() in MEDIA_EXTENSIONS and candidate.resolve().is_relative_to(root.resolve()):
                        by_name.setdefault(candidate.name, []).append(candidate)
        for item in parsed['items']:
            seen = set()
            for name in re.split(r'[\n,，;；]+', item.get('evidence','')):
                name = name.strip().replace('\\','/')
                if not name or re.match(r'https?://', name):
                    continue
                direct = (path.parent / name).resolve()
                candidates = [direct] if direct.is_relative_to(root.resolve()) and direct.is_file() else by_name.get(Path(name).name, [])
                if len(candidates) != 1:
                    parsed['warnings'].append(f"{item['case_id']}：证据“{name}”{'存在重名文件，未自动关联' if candidates else '未找到文件本体'}")
                    continue
                candidate = candidates[0]
                if candidate.suffix.lower() not in MEDIA_EXTENSIONS:
                    parsed['warnings'].append(f"{item['case_id']}：附件类型暂不支持：{candidate.name}")
                    continue
                if candidate in seen:
                    continue
                seen.add(candidate)
                attachments[item['id']].append({'name':candidate.name,'path':candidate,'content_type':mimetypes.guess_type(candidate.name)[0] or 'application/octet-stream'})
        return parsed, f'{path.name} · {page.title}', attachments
    finally:
        workbook.close()
