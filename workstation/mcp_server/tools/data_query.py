from datetime import datetime

import requests
from mcp_server.config import config
from mcp_server.decorators import ToolStatus, tool_result, tool_with_meta
from mcp_server.ros_utils import STATUS_SUCCESS, call_service, response_message, response_status
from workstation_common import get_logger

from battery_interfaces.srv import GetBatteryStatus

log = get_logger('mcp.data')


@tool_with_meta(announcement='It\'s {result.timestamp}.', )
def current_time() -> dict:
    """
    Get the current local time as `HH:MM:SS`.
    Use when the user asks for the time right now.
    """
    log.info('Current time requested')
    t = datetime.now().strftime('%H:%M:%S')
    return tool_result(ToolStatus.SUCCESS, result={'timestamp': t})


@tool_with_meta(announcement='Today is {result.date}.', )
def current_date() -> dict:
    """
    Get the current local date as `DD.MM.YYYY`.
    Use when the user asks for today's date.
    """
    log.info('Current date requested')
    t = datetime.now().strftime('%d.%m.%Y')
    return tool_result(ToolStatus.SUCCESS, result={'date': t})


@tool_with_meta(announcement='Battery is at {result.percentage}%.', )
def battery_status() -> dict:
    """
    Get current battery percentage, voltage, current draw, and charging state.
    Use when the user asks about remaining charge, charging, or battery health.
    """
    log.info('Battery status requested')
    try:
        response = call_service('/battery_node/get_status', GetBatteryStatus, GetBatteryStatus.Request(), timeout_sec=5.0)
        if response_status(response) != STATUS_SUCCESS:
            return tool_result(ToolStatus.FAILURE, error={
                'code': 'BATTERY_UNAVAILABLE',
                'message': response_message(response, 'Battery status unavailable')})
        battery_info = {
            'percentage': round(float(response.percentage), 1),
            'voltage': round(float(response.voltage), 2),
            'current_draw_amps': round(float(response.current), 2),
            'is_charging': bool(response.is_charging),
            'present': bool(response.present),
            'status_text': response.status_text,
            'health_check': response.health_check, }
        return tool_result(ToolStatus.SUCCESS, result=battery_info)
    except Exception as exc:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(exc)})


@tool_with_meta(announcement='It\'s {result.temperature_celsius}°C and {result.condition}.', )
def current_weather() -> dict:
    """
    Get the current weather for Bremen, Germany.
    Returns temperature, condition, wind speed, and humidity.
    """
    log.info('Weather requested for Bremen, Germany')
    try:
        params = {'lat': config.weather_default_lat, 'lon': config.weather_default_lon}
        response = requests.get(config.weather_api_url, params=params, timeout=5)
        response.raise_for_status()
        data = response.json()
        weather = data.get('weather', {})
        result = {
            'temperature_celsius': weather.get('temperature'),
            'condition': weather.get('condition'),
            'wind_speed_kmh': weather.get('wind_speed'),
            'humidity_percent': weather.get('relative_humidity'), }
        return tool_result(ToolStatus.SUCCESS, result=result)
    except Exception as exc:
        return tool_result(ToolStatus.FAILURE, error={'code': 'API_ERROR', 'message': str(exc)})


@tool_with_meta(announcement='Wikipedia says: {result.preview}', )
def wiki_summary(title_query: str, sentence_limit: int = 5) -> dict:
    """
    Fetch a Wikipedia intro by title.
    `title_query` should be the article title or a close title-like query.
    `sentence_limit=0` returns the full intro instead of a short summary.
    """
    log.info(f'Wikipedia extract requested for {title_query!r} with sentence_limit={sentence_limit}')
    headers = {'User-Agent': config.wiki_user_agent}
    try:
        params = {
            'action': 'query',
            'prop': 'extracts',
            'exintro': 1,
            'explaintext': 1,
            'redirects': 1,
            'titles': title_query,
            'format': 'json', }
        if sentence_limit and int(sentence_limit) > 0:
            params['exsentences'] = int(sentence_limit)
        response = requests.get(config.wiki_api_url, params=params, headers=headers, timeout=5)
        response.raise_for_status()
        payload = response.json()
        pages_by_id = (payload.get('query') or {}).get('pages') or {}
        if not pages_by_id:
            msg = f'Sorry, I couldn’t find anything on Wikipedia for: {title_query}'
            return tool_result(ToolStatus.SUCCESS, result={'title': title_query, 'extract': '', 'preview': msg})
        page_obj = next(iter(pages_by_id.values()))
        is_missing = (str(page_obj.get('pageid')) == '-1') or ('missing' in page_obj)
        if is_missing:
            msg = f'Sorry, that is not in Wikipedia: {title_query}'
            return tool_result(ToolStatus.SUCCESS, result={'title': title_query, 'extract': '', 'preview': msg})
        page_title = page_obj.get('title') or title_query
        intro_text = page_obj.get('extract') or ''
        preview_text = intro_text.strip().replace('\n', ' ')
        return tool_result(ToolStatus.SUCCESS, result={
            'title': page_title,
            'extract': intro_text,
            'preview': preview_text}, )
    except requests.exceptions.Timeout:
        msg = 'Sorry, Wikipedia is taking too long to respond.'
        return tool_result(ToolStatus.SUCCESS, result={'title': title_query, 'extract': '', 'preview': msg})
    except requests.exceptions.RequestException as exc:
        msg = f'Sorry, I couldn’t reach Wikipedia ({exc}).'
        return tool_result(ToolStatus.SUCCESS, result={'title': title_query, 'extract': '', 'preview': msg})
    except Exception as exc:
        log.error(f'Wikipedia extract failed for {title_query!r}: {exc}')
        msg = 'Sorry, something went wrong while accessing Wikipedia.'
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': msg})
