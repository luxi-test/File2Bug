from fail2bug.access import issue_review_token, review_token_remaining, review_url, REVIEW_TTL
from fail2bug.config import Settings


def test_review_token_is_scoped_signed_and_expires():
    settings=Settings(_env_file=None,access_key='team-secret')
    token=issue_review_token(settings,'batch-1',now=1000)
    assert review_token_remaining(settings,'batch-1',token,now=1001)==REVIEW_TTL-1
    assert review_token_remaining(settings,'batch-2',token,now=1001)==0
    assert review_token_remaining(settings,'batch-1',token,now=1000+REVIEW_TTL)==0
    assert review_token_remaining(settings,'batch-1','9'+token,now=1001)==0
    for bad in ('','invalid','123.汉字'):
        assert review_token_remaining(settings,'batch-1',bad,now=1001)==0
    other=Settings(_env_file=None,access_key='rotated-secret')
    assert review_token_remaining(other,'batch-1',token,now=1001)==0


def test_review_url_keeps_credentials_out_of_query_string():
    from urllib.parse import urlsplit
    settings=Settings(_env_file=None,base_url='http://192.0.2.10:8000/',access_key='team-secret')
    url=review_url(settings,'batch-1')
    parts=urlsplit(url)
    assert parts.path=='/review/batch-1' and not parts.query
    assert parts.fragment.startswith('access=')
    assert 'team-secret' not in url
