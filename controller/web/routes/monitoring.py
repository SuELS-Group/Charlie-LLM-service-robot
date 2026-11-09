from __future__ import annotations

import logging
import time

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse
from monitor import get_monitor
from web import state

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get('/monitoring', response_class=HTMLResponse)
async def serve_monitoring_page():
    page = state.STATIC_DIR / 'monitoring.html'
    if page.exists():
        return FileResponse(page)
    return HTMLResponse('')


@router.get('/api/monitoring/topics')
async def get_topic_stats():
    return (get_monitor() or _empty_monitor()).get_stats()


@router.get('/api/monitoring/network')
async def get_network_stats():
    m = get_monitor() or _empty_monitor()
    stats = m.get_stats()
    counters = m.get_network_counters()
    return {
        'interface': counters.get('interface', '') or 'unknown',
        'status': {
            'is_up': counters['is_up'],
            'speed_mbps': counters['speed_mbps'],
        },
        'counters': {
            'rx_bytes': counters['rx_bytes'],
            'tx_bytes': counters['tx_bytes'],
            'rx_packets': counters['rx_packets'],
            'tx_packets': counters['tx_packets'],
            'rx_dropped': counters['rx_dropped'],
            'tx_dropped': counters['tx_dropped'],
            'rx_errors': counters['rx_errors'],
            'tx_errors': counters['tx_errors'],
        },
        'fetched_at': time.time(),
        'history': stats.get('network_history', []),
    }


def _empty_monitor():
    from monitor import Monitor
    return Monitor("")
