import json
import asyncio
import logging
from datetime import datetime, timezone
from aiohttp import ClientError
import discord

PLAYER_CACHE = {}

logger = logging.getLogger(__name__)


async def _get_with_retry(session, url, params=None, max_retries=5, initial_backoff=1.0, backoff_factor=2.0, max_backoff=60.0):
    attempt = 0
    while True:
        try:
            async with session.get(url, params=params) as response:
                # Handle rate limiting
                if response.status == 429:
                    retry_after = response.headers.get('Retry-After')
                    try:
                        wait = int(retry_after) if retry_after is not None else int(initial_backoff * (backoff_factor ** attempt))
                    except Exception:
                        wait = initial_backoff * (backoff_factor ** attempt)
                    wait = min(wait, max_backoff)
                    logger.warning('429 from %s, retrying after %s seconds (attempt %d)', url, wait, attempt + 1)
                    await asyncio.sleep(wait)
                    attempt += 1
                    if attempt >= max_retries:
                        logger.error('Max retries reached for %s', url)
                        return None
                    continue

                # Retry on server errors
                if 500 <= response.status < 600:
                    wait = min(initial_backoff * (backoff_factor ** attempt), max_backoff)
                    logger.warning('Server error %s from %s, retrying after %s seconds (attempt %d)', response.status, url, wait, attempt + 1)
                    await asyncio.sleep(wait)
                    attempt += 1
                    if attempt >= max_retries:
                        logger.error('Max retries reached for %s', url)
                        return None
                    continue

                response.raise_for_status()
                return await response.json()

        except (ClientError, asyncio.TimeoutError) as e:
            wait = min(initial_backoff * (backoff_factor ** attempt), max_backoff)
            logger.warning('Network error contacting %s: %s; retrying in %s seconds (attempt %d)', url, e, wait, attempt + 1)
            await asyncio.sleep(wait)
            attempt += 1
            if attempt >= max_retries:
                logger.exception('Max retries reached for %s: %s', url, e)
                return None
        except Exception:
            logger.exception('Unexpected error contacting %s', url)
            return None

async def get_user(username, session, base_url="https://api2.warera.io/trpc/search.searchAnything"):
    try:
        if username in PLAYER_CACHE.keys():
            user = await get_user_info(PLAYER_CACHE[username], session)
            return user

        input_data = {'searchText': username}
        params = {"input": json.dumps(input_data)}
        data = await _get_with_retry(session, base_url, params=params)
        if not data:
            return None
        api_result = data.get('result', {}).get('data')
        if not api_result or api_result.get('hasData') is False:
            return None
        for userId in api_result.get('userIds', []) or []:
            user = await get_user_info(userId, session)
            if user is None:
                continue
            if username == user.get('username'):
                PLAYER_CACHE[username] = user.get('_id')
                return user
        return None
    except Exception:
        return None

async def get_user_info(userId, session, base_url="https://api2.warera.io/trpc/user.getUserLite"):
    try:
        input_data = {'userId': userId}
        params = {"input": json.dumps(input_data)}
        data = await _get_with_retry(session, base_url, params=params)
        if not data:
            return None
        api_result = data.get('result', {}).get('data')
        if not api_result:
            return None
        return api_result
    except Exception:
        return None

async def get_all_countries(session, base_url="https://api2.warera.io/trpc/country.getAllCountries"):
    try:
        data = await _get_with_retry(session, base_url)
        if not data:
            return None
        api_result = data.get('result', {}).get('data')
        if not api_result:
            return None
        return api_result
    except Exception:
        return None

async def get_country_government(counrtyId, session, base_url="https://api2.warera.io/trpc/government.getByCountryId"):
    try:
        input_data = {'countryId': counrtyId}
        params = {"input": json.dumps(input_data)}
        data = await _get_with_retry(session, base_url, params=params)
        if not data:
            return None
        api_result = data.get('result', {}).get('data')
        if not api_result:
            return None
        return api_result
    except Exception:
        return None

async def get_fight_status(userId: str, session, member: discord.Member | None = None, base_url: str = "https://api2.warera.io/trpc/user.getUserLite") -> dict | None:
    """Fetch lightweight user info and return a dict with fight-related fields.

    Returns a dict or None on failure. Dict keys:
    - userId, warera_name, display_name, avatar_url, level, is_active,
      health_curr, health_total, hunger_curr, hunger_total, buff_text
    """
    try:
        api_result = await get_user_info(userId, session, base_url=base_url)
        if not api_result:
            return None

        leveling = api_result.get('leveling', {})
        level = leveling.get('level', 'N/A')
        is_active = api_result.get('isActive', False)

        skills = api_result.get('skills', {}) or {}
        health = skills.get('health', {}) or {}
        hunger = skills.get('hunger', {}) or {}

        health_curr = health.get('currentBarValue')
        health_total = health.get('total')
        hunger_curr = hunger.get('currentBarValue')
        hunger_total = hunger.get('total')

        buffs = api_result.get('buffs') or {}
        buff_text = "No buff/debuff"
        # Prefer debuffEndAt when present, otherwise fall back to buffEndAt
        buff_end_at = None
        buff_type = None
        buff_active = False
        if isinstance(buffs, dict) and buffs:
            if 'debuffEndAt' in buffs and buffs.get('debuffEndAt'):
                buff_end_at = buffs.get('debuffEndAt')
                buff_type = 'Debuff'
            elif 'buffEndAt' in buffs and buffs.get('buffEndAt'):
                buff_end_at = buffs.get('buffEndAt')
                buff_type = 'Buff'

            if buff_end_at:
                try:
                    buff_dt = datetime.fromisoformat(buff_end_at.replace('Z', '+00:00'))
                    now = datetime.now(timezone.utc)
                    remaining = buff_dt - now
                    buff_active = remaining.total_seconds() > 0
                    if buff_active:
                        days = remaining.days
                        hours = remaining.seconds // 3600
                        minutes = (remaining.seconds % 3600) // 60
                        # Provide relative time only (e.g. "Buff ends in 1d 2h 3m")
                        buff_text = f"{buff_type} ends in {days}d {hours}h {minutes}m"
                    else:
                        buff_text = f"{buff_type} expired"
                except Exception:
                    buff_active = False
                    buff_text = f"{buff_type}: {buff_end_at}"

        # avatar (may be unused by callers)
        avatar_url = None
        display_name = None
        if member is not None:
            try:
                display_name = member.display_name
                asset = member.display_avatar
                try:
                    avatar_url = str(asset.with_size(64))
                except Exception:
                    avatar_url = getattr(asset, 'url', None)
            except Exception:
                display_name = None

        return {
            'userId': userId,
            'warera_name': api_result.get('username'),
            'display_name': display_name,
            'avatar_url': avatar_url,
            'level': level,
            'is_active': is_active,
            'health_curr': health_curr,
            'health_total': health_total,
            'hunger_curr': hunger_curr,
            'hunger_total': hunger_total,
            'buff_text': buff_text,
            'buff_type': buff_type,
            'buff_end_at': buff_end_at,
            'buff_active': bool(buff_active),
        }
    except Exception:
        return None