import time
from src.scanner import RecentCandidatesCache

def test_recent_candidates_cache_basic():
    cache = RecentCandidatesCache(ttl_sec=2)
    now = time.time()
    cache.on_candidate('BTCUSDT', True, now)
    assert 'BTCUSDT' in cache._data
    cache.on_candidate('BTCUSDT', True, now+1)
    assert cache._data['BTCUSDT']['last_seen_at'] == now+1
    cache.on_candidate('BTCUSDT', False, now+2)
    assert cache._data['BTCUSDT']['last_seen_at'] == now+2
    cache.cleanup()
    assert 'BTCUSDT' in cache._data
    time.sleep(4.1)
    cache.cleanup()
    assert 'BTCUSDT' not in cache._data

def test_recent_candidates_cache_filter():
    cache = RecentCandidatesCache(ttl_sec=10)
    now = time.time()
    cache.on_candidate('BTCUSDT', True, now)
    cache.on_candidate('ETHUSDT', True, now-400)
    rate_map = {'BTCUSDT': 8.0, 'ETHUSDT': 6.0}
    recent = cache.get_recent(max_age_min=5, min_rate=7.2, rate_map=rate_map)
    assert any(r['symbol']=='BTCUSDT' for r in recent)
    assert not any(r['symbol']=='ETHUSDT' for r in recent)
