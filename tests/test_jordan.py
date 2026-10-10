import asyncio
import json
from argparse import Namespace
from unittest.mock import AsyncMock, patch
import httpx
import pytest
import market
import opensooq_client as oc
import server as s
import watch
import install


def listing(ident='1', title='ايفون 15 برو', price=300, currency='JOD', reviews=20):
    return oc.normalize_item({'id':ident, 'title':title, 'price_amount':price,
                             'price_currency_iso':currency, 'member_id':5,
                             'verification_level':1, 'member_rating_avg':4.5,
                             'member_rating_count':reviews, 'post_url':f'/search/{ident}'})


def test_jordan_catalog_and_arabic_lookup():
    assert oc.COUNTRY == 'jo' and oc.CURRENCY == 'JOD'
    assert len(oc.CATEGORIES) == 17 and len(oc.CITIES) == 14
    assert market.resolve_city('عَمّان') == market.resolve_city('Amman') == 59
    assert market.resolve_city('إربد') == 60
    assert market.resolve_category('سيارات ومركبات') == 1775
    assert market.resolve_category('موبايل - تابلت') == 5505
    with pytest.raises(ValueError, match='Ambiguous'):
        market.resolve_category('Property')
    with pytest.raises(ValueError):
        market.resolve_city('50')

@pytest.mark.parametrize('raw,expected', [('١٬٢٥٠٫٥٠٠ د.أ',1250.5), ('۱۲۳٫۴۵ JOD',123.45),
                                         ('400.125 JOD',400.125), ('-10',0), ('nan',0)])
def test_price_parsing(raw, expected):
    assert oc.parse_price(raw) == expected


def test_arabic_digit_and_modifier_relevance():
    matches = oc.relevant_listings('آيفون ١٥ برو', [listing(), listing('2','ايفون 14 برو'), listing('3','ايفون 15')])
    assert [x['id'] for x in matches] == ['1']
    assert oc.relevant_listings('professional camera', [listing(title='professional camera')])


def test_statistics_and_currency_isolation():
    assert s._price_stats([100,200,300,400])['p25'] == 175
    assert s._price_stats([100,200,300,400])['p75'] == 325
    comparable, _, _ = oc.comparable_listings([listing(),listing('2',price=1),listing('3',currency='USD')])
    assert all(x['currency'] == 'JOD' for x in comparable)


def test_invalid_price_range():
    with pytest.raises(ValueError):
        s.SearchInput(query='ايفون', min_price=500, max_price=100)


def test_search_paginates_filtered_sample_and_empty_json():
    items = [listing(str(i), price=100+i) for i in range(6)]
    with patch.object(s, '_relevance_gated_fetch', AsyncMock(return_value=(items, {'count':100}))):
        result = json.loads(asyncio.run(s.opensooq_search_listings(s.SearchInput(query='ايفون 15 برو', sort='cheapest', page=2,limit=2,response_format='json'))))
        assert [x['id'] for x in result['listings']] == ['2','3']
        assert result['page'] == 2 and result['sample_count'] == 6
        assert 'sample_note' in result
        empty = json.loads(asyncio.run(s.opensooq_search_listings(s.SearchInput(query='ايفون 15 برو', sort='cheapest',page=50,response_format='json'))))
        assert empty['count'] == 0


def test_sellers_require_review_evidence():
    with patch.object(oc,'search_multi_page',AsyncMock(return_value=[listing(reviews=1)])):
        result=json.loads(asyncio.run(s.opensooq_find_trusted_sellers(s.TrustedSellersInput(query='ايفون 15 برو'))))
        assert result['seller_count'] == 0
        assert result['criteria']['min_reviews'] == 5


def test_pagination_deduplicates_and_stops():
    fetch=AsyncMock(side_effect=[([listing('1')],{'pages':2}),([listing('1'),listing('2')],{'pages':2})])
    with patch.object(oc,'search_page',fetch):
        result=asyncio.run(oc.search_multi_page('ايفون',pages=5))
    assert [x['id'] for x in result] == ['1','2'] and fetch.await_count == 2


def test_anonymous_handshake_jordan_and_retry():
    seen=[]
    def handle(request):
        seen.append(request)
        if request.url.path == '/api/register-device':
            return httpx.Response(200,json={'deviceUUID':'test','t':1,'k':'test-only'})
        assert request.headers['country'] == 'jo'
        assert request.headers['currency'] == 'JOD'
        assert 'ecountry=jo' in request.headers['cookie']
        return httpx.Response(200,json={'listings':{'items':[],'meta':{'count':0}}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with patch.object(oc,'_cached_reg',None):
                await oc.raw_search('ايفون',client=client)
    asyncio.run(run())
    assert all(r.url.host=='jo.opensooq.com' for r in seen)


def test_retry_after_and_schema_drift():
    responses=[httpx.Response(429,headers={'Retry-After':'0'}),httpx.Response(200,json={'listings':{'items':[]}})]
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: responses.pop(0))) as client:
            with patch.object(oc,'_get_registration',AsyncMock(return_value=oc._Registration('test',1,'test'))):
                result=await oc.raw_search('ايفون',client=client)
                assert 'listings' in result
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'error':'changed'}))) as client:
            with patch.object(oc,'_get_registration',AsyncMock(return_value=oc._Registration('test',1,'test'))):
                with pytest.raises(oc.OpenSooqError,match='schema'):
                    await oc.raw_search('ايفون',client=client)
    asyncio.run(run())


def test_detail_fetch_rejects_external_urls():
    client=AsyncMock()
    assert asyncio.run(oc.fetch_listing_page_details('https://example.com/steal',client=client)) == {}
    assert asyncio.run(oc.fetch_listing_page_details('//example.com/steal',client=client)) == {}
    client.get.assert_not_called()


def test_watcher_price_drops_and_no_duplicates():
    alerts,state=watch.select_alerts([listing(price=300)], {'prices':{}})
    assert alerts[0]['alert_type']=='new_match'
    assert watch.select_alerts([listing(price=300)],state)[0]==[]
    alerts,state=watch.select_alerts([listing(price=280)],state)
    assert alerts[0]['alert_type']=='price_drop' and alerts[0]['previous_price']==300
    assert watch.select_alerts([listing(price=280)],state)[0]==[]
    assert watch.select_alerts([listing(price=290)],state)[0]==[]
    assert watch.select_alerts([listing(price=285)],state)[0]==[]


def test_watcher_migration_and_corruption_preserved(tmp_path):
    path=tmp_path/'state.json';path.write_text('["1"]')
    alerts,state=watch.select_alerts([listing()],watch.load_state(path))
    assert alerts==[] and state['prices']['1']==300
    path.write_text('{bad')
    with pytest.raises(ValueError): watch.load_state(path)
    assert path.read_text()=='{bad'


def test_failed_webhook_raises():
    fake=httpx.Response(500,request=httpx.Request('POST','https://example.com'))
    client=AsyncMock();client.post.return_value=fake
    with patch.object(watch.httpx,'AsyncClient') as constructor:
        constructor.return_value.__aenter__.return_value=client
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(watch.post_webhook('https://example.com','ايفون',[listing()]))


def test_installer_print_only_does_not_provision(capsys):
    with patch('sys.argv',['install.py','--print-only']),patch.object(install,'find_uv',return_value='/bin/uv'),patch.object(install,'ensure_runtime') as provision:
        assert install.main()==0
        provision.assert_not_called()
    assert 'OPENSOOQ_COUNTRY' in capsys.readouterr().out


def test_mcp_lists_all_tools_with_readonly_annotations():
    tools=asyncio.run(s.mcp.list_tools())
    assert len(tools)==15
    assert all(tool.annotations.readOnlyHint for tool in tools)


def test_enrichment_follows_only_same_market_redirects():
    seen=[]
    def handle(request):
        seen.append(str(request.url))
        if request.url.path=='/search/1':
            return httpx.Response(302,headers={'Location':'/en/post/1'})
        return httpx.Response(302,headers={'Location':'https://example.com/private'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            assert await oc.fetch_listing_page_details('/search/1',client=client)=={}
    asyncio.run(run())
    assert len(seen)==2 and all(url.startswith(oc.BASE_URL) for url in seen)


def test_authentication_retries_once():
    requests=[]
    def handle(request):
        requests.append(request)
        if request.url.path=='/api/register-device':
            return httpx.Response(200,json={'deviceUUID':'test','t':1,'k':'test'})
        if sum(r.url.path=='/api/serp/search/v2' for r in requests)==1:
            return httpx.Response(401)
        return httpx.Response(200,json={'listings':{'items':[]}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with patch.object(oc,'_cached_reg',None):
                await oc.raw_search('ايفون',client=client)
    asyncio.run(run())
    assert sum(r.url.path=='/api/register-device' for r in requests)==2


def test_single_listing_rejects_unrelated_api_fallback():
    with patch.object(oc,'raw_search',AsyncMock(return_value={'listings':{'items':[{'id':999}]}})):
        assert asyncio.run(oc.fetch_listing_by_id('1')) is None


def test_equal_price_histogram_has_valid_bounds():
    with patch.object(oc,'search_multi_page',AsyncMock(return_value=[listing(),listing('2')])):
        result=json.loads(asyncio.run(s.opensooq_price_distribution(s.PriceDistributionInput(query='ايفون 15 برو'))))
        assert sum(b['count'] for b in result['buckets'])==2
        assert all(b['range_low']<=b['range_high'] for b in result['buckets'])


def test_real_stdio_mcp_handshake():
    import os
    import sys
    from pathlib import Path
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    async def run():
        params=StdioServerParameters(command=sys.executable,args=[str(Path(s.__file__).resolve())],
                                    env={**os.environ,'OPENSOOQ_COUNTRY':'jo'})
        async with stdio_client(params) as (reader,writer):
            async with ClientSession(reader,writer) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools)==15
                response=await session.call_tool('opensooq_list_cities',{})
                cities=json.loads(response.content[0].text)
                assert cities['country']=='jo' and cities['count']==14
    asyncio.run(run())
