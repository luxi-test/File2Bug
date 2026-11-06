import pytest

from fail2bug.titles import suggest_title


@pytest.mark.parametrize('case,actual,expected', [
    ('验证码登录', '点击登录后无响应', '点击登录后无响应'),
    ('验证码输入错误时显示提示', '没有任何提示', '验证码输入错误时没有任何提示'),
    ('验证登录成功后跳转首页', '仍停留在登录页', '登录成功后仍停留在登录页'),
    ('退出登录', '没有反应', '退出登录：没有反应'),
    ('保存照片', '照片没有保存', '照片没有保存'),
    ('关闭推送后不再收到提醒', '仍收到离线提醒', '关闭推送后仍收到离线提醒'),
    ('头像修改后首页同步', '资料页头像更新成功，首页仍显示旧头像。', '首页仍显示旧头像'),
    ('设备重连', '重新连接后仍显示旧名称，退出页面再进入才更新。', '重新连接后仍显示旧名称'),
    ('打开设置页正常显示', '闪退', '打开设置页：闪退'),
    ('登录', '已提单，点击登录按钮无响应', '点击登录按钮无响应'),
    ('登录', '1. 点击登录按钮无响应\n2. 重新进入后正常', '点击登录按钮无响应'),
    ('登录', '前两步正常。点击登录无响应。重启后正常。', '点击登录无响应'),
    ('登录', 'App crashes after tapping Login. Restart helps.', 'App crashes after tapping Login'),
    ('角色显示正确', '账号为男性，页面显示女士称谓', '账号为男性，页面显示女士称谓'),
    ('验证方式切换', '没有提示', '验证方式切换：没有提示'),
    ('关闭后台任务', '没有反应', '关闭后台任务：没有反应'),
    ('设置前景颜色', '没有保存', '设置前景颜色：没有保存'),
    ('页面时间显示', '没有更新', '页面时间显示：没有更新'),
])
def test_title_reports_actual_symptom_and_necessary_context(case, actual, expected):
    result = suggest_title(case, actual)
    assert result['value'] == expected
    assert not result['needs_review']
    assert result['source'] == 'actual'


@pytest.mark.parametrize('actual', ['', 'fail', '已提单', '失败', '见截图', '如图', '异常',
                                    '无法复现', '未发现异常', 'https://example.test/evidence', 'evidence.mp4'])
def test_insufficient_actual_is_not_inverted_from_test_expectations(actual):
    result = suggest_title('首次进入页面默认展示角色', actual)
    assert result['value'] == '首次进入页面默认展示角色'
    assert result['needs_review']
    assert result['source'] == 'case_title'


def test_module_prefix_is_not_reintroduced_and_long_result_is_only_abbreviated():
    result = suggest_title('[设置] 保存配置', '[设置] 配置保存失败', '设置')
    assert result['value'] == '配置保存失败'
    long = '配置保存失败，' + '同一实际记录中的详细表现' * 20
    result = suggest_title('保存配置', long)
    assert len(result['value']) <= 120
    assert '原因' not in result['value']


def test_negated_crash_is_not_chosen_over_the_reported_failure():
    result = suggest_title('登录', '没有崩溃。点击登录按钮无响应。')
    assert result['value'] == '点击登录按钮无响应'
