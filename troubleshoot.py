#!/usr/bin/env python3
"""Small, explicit live smoke check. No network calls unless --live is supplied."""
import argparse
import asyncio
import json
import server as s
import opensooq_client as oc

async def check():
    if oc.COUNTRY != 'jo':
        raise ValueError('This smoke check requires OPENSOOQ_COUNTRY=jo')
    cities=json.loads(await s.opensooq_list_cities())
    categories=json.loads(await s.opensooq_list_categories())
    assert cities['country']=='jo' and categories['country']=='jo'
    result=json.loads(await s.opensooq_search_listings(s.SearchInput(
        query='ايفون', city='عمان', category='موبايل - تابلت', limit=3, response_format='json')))
    assert result['count']>0, result
    assert all(x['currency']=='JOD' and x['location']['city']=='Amman' for x in result['listings'])
    first=result['listings'][0]
    detail=json.loads(await s.opensooq_get_listing(s.GetListingInput(
        listing_id=first['id'], include_details=True, response_format='json')))
    assert detail['listing']['id']==first['id'], detail
    summary=json.loads(await s.opensooq_market_summary(s.MarketSummaryInput(
        query='ايفون',category='موبايل - تابلت',pages=1)))
    assert summary['currency']=='JOD' and summary['priced_listings']>0, summary
    print(json.dumps({'status':'PASS','country':oc.COUNTRY,'currency':oc.CURRENCY,
                      'city_count':cities['count'],'category_count':categories['count'],
                      'search_count':result['count'],'listing_lookup':'PASS','detail_enrichment':bool(detail.get('details')),
                      'price_sample_count':summary['priced_listings'],
                      'tool_count':len(await s.mcp.list_tools())},indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='Make a small number of live OpenSooq requests')
    args=parser.parse_args()
    if args.live:
        asyncio.run(check())
    else:
        parser.print_help()
