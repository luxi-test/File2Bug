"""Associate Feishu cell media and floating images with failed case rows."""
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from .feishu import FeishuError


def cell_media(value):
    if isinstance(value, list):
        for part in value:
            yield from cell_media(part)
    elif isinstance(value, dict):
        token = value.get("fileToken") or value.get("file_token")
        if isinstance(token, str) and token:
            name = value.get("fileName") or value.get("name") or value.get("text") or "证据"
            yield token, str(name)
        else:
            for part in value.values():
                if isinstance(part, (list, dict)):
                    yield from cell_media(part)


def attach_sheet_media(client, settings, parsed, rows, canonical, attachment_path):
    by_row = {item["row_no"]: item for item in parsed["items"]}
    if not by_row:
        return []
    warnings = parsed.setdefault("warnings", [])
    references = {row: {} for row in by_row}
    width = max(len(row) for row in rows)

    try:
        for row_no, cells in client.read_sheet_media(canonical, set(by_row), width, len(rows)):
            for token, name in cell_media(cells):
                references[row_no].setdefault(token, name)
    except FeishuError as error:
        warnings.append("单元格附件读取未完成：" + str(error))
    try:
        for image in client.read_float_images(canonical):
            match = re.fullmatch(r"[^!]+!([A-Z]+)(\d+):\1\2", image.get("range", ""))
            token = image.get("float_image_token")
            if match and token and int(match[2]) in by_row:
                references[int(match[2])].setdefault(token, "浮动截图")
    except FeishuError as error:
        warnings.append("浮动图片读取未完成：" + str(error))

    cached = []
    remaining = settings.max_import_mb * 1024 * 1024
    downloads = {}
    try:
        with TemporaryDirectory(prefix="f2b-sheet-media-") as temporary:
            for row_no, media in references.items():
                item = by_row[row_no]
                for token, name in media.items():
                    if len(item["attachments"]) >= 10 or remaining <= 0:
                        warnings.append(f"{item['case_id']}：附件超过数量或批次大小上限，等待补充")
                        continue
                    if token not in downloads:
                        path = Path(temporary) / uuid4().hex
                        try:
                            metadata = client.download_media(
                                token, path, name,
                                min(settings.max_attachment_mb * 1024 * 1024, remaining),
                            )
                            downloads[token] = (path, metadata)
                        except FeishuError as error:
                            downloads[token] = str(error)
                    download = downloads[token]
                    if isinstance(download, str):
                        warnings.append(f"{item['case_id']}：{download}")
                        continue
                    path, metadata = download
                    if metadata["size"] > remaining:
                        warnings.append(f"{item['case_id']}：附件超过批次大小上限，等待补充")
                        continue
                    storage_name = uuid4().hex
                    target = attachment_path(storage_name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    cached.append(target)
                    shutil.copyfile(path, target)
                    target.chmod(0o600)
                    item["attachments"].append({
                        "id": str(uuid4()), **metadata, "storage_name": storage_name, "linked": False,
                    })
                    remaining -= metadata["size"]
    except BaseException:
        for path in cached:
            path.unlink(missing_ok=True)
        raise
    return cached
