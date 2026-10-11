"""Request-scoped work reuse and bounded routing for EcoTrace."""
from contextvars import ContextVar
from contextlib import contextmanager
from functools import wraps
from threading import BoundedSemaphore, Lock, Thread, local
from collections import OrderedDict
import time

_state = ContextVar('ecotrace_route_work', default=None)
_slots = BoundedSemaphore(2)
_weather_lock = Lock()
_weather_pending = set()
_rasters = local()


def route_work(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not _slots.acquire(blocking=False):
            raise RuntimeError('Two routes are already calculating. Wait for one to finish, then try again.')
        started = time.monotonic()
        state = {'started': started, 'calls': 0, 'memo': {}}
        token = _state.set(state)
        try:
            return fn(*args, **kwargs)
        finally:
            print(f'EcoTrace {fn.__name__}: {time.monotonic()-started:.2f}s, ORS calls={state["calls"]}', flush=True)
            _state.reset(token)
            _slots.release()
    return wrapped


def check_budget():
    state = _state.get()
    if state and time.monotonic() - state['started'] > 45:
        raise RuntimeError('Route analysis exceeded its processing budget. Try a shorter route.')


def ors_timeout():
    state = _state.get()
    if state is None:
        return (3, 12)
    remaining = 24 - (time.monotonic() - state['started'])
    if state['calls'] >= 4 or remaining < 1:
        raise RuntimeError('Routing service request budget reached. Try endpoints on a mapped street or path.')
    state['calls'] += 1
    return (min(3, remaining / 3), min(12, remaining * 2 / 3))


def memoized_work(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        check_budget()
        state = _state.get()
        if state is None:
            return fn(*args, **kwargs)
        # Exact arguments: no geometry simplification or changed shade samples.
        key = (fn.__name__, repr(args), repr(sorted(kwargs.items())))
        memo = state['memo']
        if key not in memo:
            memo[key] = fn(*args, **kwargs)
        return memo[key]
    return wrapped


def route_weather(fetch, snapshot_type, cache, lat, lon):
    key = (round(lat, 3), round(lon, 3))
    cached = cache.get(key)
    if cached and time.time() - cached[0] < 300:
        return cached[1]
    # Weather never holds a route response hostage. Limit and deduplicate refreshes.
    with _weather_lock:
        if key not in _weather_pending and len(_weather_pending) < 2:
            _weather_pending.add(key)
            def refresh():
                try:
                    fetch(*key)
                except Exception:
                    pass
                finally:
                    with _weather_lock:
                        _weather_pending.discard(key)
            Thread(target=refresh, daemon=True, name='ecotrace-weather').start()
    return snapshot_type(available=False, temperature_f=None,
        precipitation_probability=None, wind_speed_mph=None,
        source='NWS weather refresh pending',
        warning='Current weather is loading separately; it was not used in this route.',
        impact_summary='Route geometry and shade analysis are available without waiting for weather.')


@contextmanager
def raster_dataset(path, rasterio):
    # GDAL handles are reused only on their owning thread, never across threads.
    cache = getattr(_rasters, 'handles', None)
    if cache is None:
        cache = _rasters.handles = OrderedDict()
    key = str(path.resolve())
    stat = path.stat()
    version = (stat.st_mtime_ns, stat.st_size)
    previous = cache.pop(key, None)
    if previous and previous[0] == version and not previous[1].closed:
        dataset = previous[1]
    else:
        if previous:
            previous[1].close()
        dataset = rasterio.open(path)
    cache[key] = (version, dataset)
    while len(cache) > 4:
        cache.popitem(last=False)[1][1].close()
    yield dataset
