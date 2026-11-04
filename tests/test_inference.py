import pytest

from fail2bug.inference import infer_frequency, infer_item, infer_problem_type, FREQUENCIES
from fail2bug.repro import format_repro


@pytest.mark.parametrize('text,expected',[
    ('只有一条 fail',''),('复现一次',''),('无法稳定复现',''),('不是必现',''),
    ('每次都出现',FREQUENCIES['very_high']),('稳定复现',FREQUENCIES['very_high']),
    ('偶现',FREQUENCIES['medium']),('低概率发生',FREQUENCIES['low']),
    ('复现 3/5 次',FREQUENCIES['high']),('复现率：5%',FREQUENCIES['low']),
])
def test_frequency_requires_evidence(text,expected):
    assert infer_frequency(text)[0]==expected


def test_expected_does_not_drive_bug_type():
    item={'expected':'不应闪退','actual':'按钮无法点击'}
    value,_=infer_problem_type(item,['稳定性问题','交互缺陷'])
    assert value=='交互缺陷'
    assert infer_problem_type({'actual':'已提单'},['功能异常'])[0]==''


def test_submodule_falls_back_to_module_and_unknown_frequency_uses_default():
    item={'module':'设备页与设备记录','title':'修改名称','actual':'保存后名称没有更新'}
    infer_item(item,{'problem_type':{'options':['功能异常']},'submodule':{'options':['查看照片']},'frequency':{'options':list(FREQUENCIES.values())}})
    assert item['submodule']=='设备页与设备记录'
    assert item['inference']['submodule']['source']=='module_fallback'
    assert item['problem_type']=='功能异常'
    assert item['frequency']==FREQUENCIES['very_high']
    assert item['inference']['frequency']['source']=='default'


def test_repro_has_exact_four_sections_and_retains_legacy_premise():
    item={'steps':'前置条件\n设备已连接\n\n测试数据\n图片A\n\n操作步骤\n1. 点击拍照','expected':'保存图片','actual':'未保存'}
    assert format_repro(item)=='【前提】：\n设备已连接\n测试数据：图片A\n\n【操作】：\n1. 点击拍照\n\n【预期结果】：\n保存图片\n\n【实际结果】：\n未保存'


def test_unrelated_percent_is_not_occurrence_rate():
    assert infer_frequency('电量 100% 时仍保存失败')[0]==''
    item={'module':'相册','actual':'没有保存','frequency_evidence':'100%'}
    infer_item(item,{'frequency':{'options':list(FREQUENCIES.values())}})
    assert item['frequency']==FREQUENCIES['very_high']
