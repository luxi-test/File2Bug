import pytest

from fail2bug.feishu import column_name, parse_sheet_url
from fail2bug.parser import InputError, demo_table, normalize_result, parse_table


@pytest.mark.parametrize("value,expected", [("失败（Fail）", "fail"), (" PASS ", "pass"), ("⭕", "unknown"), ("通过(但失败)", "unknown"), (None, "not_run"), ("环境问题", "blocked")])
def test_explicit_result_values(value, expected):
    assert normalize_result(value) == expected


def test_all_failures_kept_and_actual_not_invented():
    result = parse_table(demo_table(), "")
    assert len(result["items"]) == 5
    assert sum(result["counts"].values()) == 8
    assert result["counts"]["unknown"] == 1
    missing = next(item for item in result["items"] if item["case_id"] == "TC-067")
    assert missing["actual"] == ""
    assert "请补充实际结果" in missing["issues"]
    assert "重新连接" in result["items"][0]["title"]


def test_column_order_and_source_row_numbers():
    table = demo_table()
    reordered = [["说明", *reversed(row)] for row in table]
    result = parse_table([["回归测试"], [], *reordered], "")
    assert result["items"][0]["case_id"] == "TC-021"
    assert result["items"][0]["row_no"] == 4


def test_missing_and_duplicate_headers_rejected():
    with pytest.raises(InputError, match="缺少列"):
        parse_table([["标题"], ["失败"]], "")
    rows = demo_table()
    rows[0].append("标题")
    with pytest.raises(InputError, match="表头重复"):
        parse_table(rows, "")


@pytest.mark.parametrize("url", ["https://evil.test/sheets/abc", "https://team.feishu.cn.evil.test/sheets/abc", "http://team.feishu.cn/sheets/abc", "https://team.feishu.cn/wiki/abc", "https://user:pw@team.feishu.cn/sheets/abc"])
def test_rejects_unsupported_urls(url):
    with pytest.raises(InputError):
        parse_sheet_url(url)


def test_sheet_url_and_columns():
    assert parse_sheet_url("https://team.feishu.cn/sheets/abc123?sheet=xyz") == ("abc123", "xyz")
    assert [column_name(value) for value in [1, 26, 27, 100]] == ["A", "Z", "AA", "CV"]
