from io import BytesIO
from pathlib import Path
import zipfile

from openpyxl import Workbook
from openpyxl.drawing.image import Image as ExcelImage
from PIL import Image
import pytest

from fail2bug.config import Settings
from fail2bug.feishu import FeishuClient
from fail2bug.local_source import open_local
from fail2bug.parser import InputError
from fail2bug.service import Service
from fail2bug.store import Store


def workbook(path,evidence='',embedded=False):
    book=Workbook();sheet=book.active;sheet.title='执行结果'
    sheet.append(['用例ID','PRD模块','标题','优先级','操作步骤','预期结果','执行结果','备注','证据'])
    sheet.append(['LOCAL-1','相册','照片保存','P1','点击拍照','保存图片','fail','图片没有保存，必现',evidence])
    if embedded:
        image=BytesIO();Image.new('RGB',(2,2),'red').save(image,format='PNG');image.seek(0)
        sheet.add_image(ExcelImage(image),'I2')
    book.save(path);book.close()


def service(tmp_path):
    settings=Settings(_env_file=None,database=tmp_path/'state.db')
    return Service(settings,Store(settings.database),FeishuClient(settings))


def test_embedded_picture_attached_to_failure_row(tmp_path):
    path=tmp_path/'执行表.xlsx';workbook(path,embedded=True)
    agent=service(tmp_path)
    batch=agent.import_file(path)
    assert len(batch['items'])==1
    assert batch['items'][0]['frequency']=='Very High / Frequent'
    assert len(batch['items'][0]['attachments'])==1
    assert agent.attachment_path(batch['items'][0]['attachments'][0]['storage_name']).is_file()


def test_zip_relative_evidence_files_are_cached(tmp_path):
    root=tmp_path/'source';root.mkdir()
    book=root/'执行表.xlsx';workbook(book,'evidence/recording.mp4')
    archive=tmp_path/'输入.zip'
    with zipfile.ZipFile(archive,'w') as bundle:
        bundle.write(book,'执行表.xlsx');bundle.writestr('evidence/recording.mp4',b'video fixture')
    agent=service(tmp_path/'output')
    batch=agent.import_file(archive)
    attachment=batch['items'][0]['attachments'][0]
    assert attachment['name']=='recording.mp4'
    assert agent.attachment_path(attachment['storage_name']).read_bytes()==b'video fixture'
    assert batch['warnings']==[]


def test_missing_or_ambiguous_files_reported(tmp_path):
    book=tmp_path/'执行表.xlsx';workbook(book,'same.png\nmissing.mp4')
    for directory in ['a','b']:
        (tmp_path/directory).mkdir();(tmp_path/directory/'same.png').write_bytes(b'image')
    batch=service(tmp_path/'output').import_file(book)
    assert not batch['items'][0]['attachments']
    assert any('重名' in warning for warning in batch['warnings'])
    assert any('未找到' in warning for warning in batch['warnings'])


def test_zip_traversal_rejected(tmp_path):
    archive=tmp_path/'unsafe.zip'
    with zipfile.ZipFile(archive,'w') as bundle:bundle.writestr('../outside.txt','no')
    with pytest.raises(InputError,match='不安全'):
        with open_local(archive,max_bytes=1024*1024):pass
    assert not (tmp_path.parent/'outside.txt').exists()


def test_relative_hyperlink_uses_actual_evidence_target(tmp_path):
    from openpyxl import load_workbook
    path=tmp_path/'执行表.xlsx';workbook(path,'查看截图')
    book=load_workbook(path);book.active['I2'].hyperlink='shot.png';book.save(path);book.close()
    (tmp_path/'shot.png').write_bytes(b'image fixture')
    batch=service(tmp_path/'output').import_file(path)
    assert batch['items'][0]['attachments'][0]['name']=='shot.png'


def test_actual_result_column_and_wps_cell_images_import_together(tmp_path):
    path = tmp_path / '执行表.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.append(['用例ID', 'PRD模块', '标题', '优先级', '操作步骤', '预期结果', '执行结果', '实际结果', '证据', '备注'])
    formula = '=_xlfn.DISPIMG("ID_screen",1)'
    sheet.append(['CASE-1', '相册', '照片保存', 'P1', '点击拍照', '保存图片', 'fail', '图片没有保存', formula, '辅助备注'])
    sheet.append(['CASE-2', '相册', '照片保存', 'P1', '点击拍照', '保存图片', 'fail', None, formula, '图片闪退'])
    book.save(path)
    book.close()

    picture = BytesIO()
    Image.new('RGB', (2, 2), 'red').save(picture, format='PNG')
    with zipfile.ZipFile(path, 'a') as archive:
        archive.writestr('xl/cellimages.xml', '''<cellImages
          xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
          xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
          <cellImage><xdr:pic><xdr:nvPicPr><xdr:cNvPr name="ID_screen"/></xdr:nvPicPr>
          <xdr:blipFill><a:blip r:embed="rId1"/></xdr:blipFill></xdr:pic></cellImage>
          </cellImages>''')
        archive.writestr('xl/_rels/cellimages.xml.rels', '''<Relationships
          xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
          Target="media/screen.png"/></Relationships>''')
        archive.writestr('xl/media/screen.png', picture.getvalue())

    agent = service(tmp_path / 'output')
    batch = agent.import_file(path)
    assert [item['actual'] for item in batch['items']] == ['图片没有保存', '图片闪退']
    for item in batch['items']:
        attachment, = item['attachments']
        assert attachment['content_type'] == 'image/png'
        assert agent.attachment_path(attachment['storage_name']).read_bytes() == picture.getvalue()
        assert not item['evidence']
    assert batch['warnings'] == []
