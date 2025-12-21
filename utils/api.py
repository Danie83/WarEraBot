import json

PLAYER_CACHE = {}

async def get_user(username, session, base_url="https://api2.warera.io/trpc/search.searchAnything"):
    if username in PLAYER_CACHE.keys():
        user = await get_user_info(PLAYER_CACHE[username], session)
        return user

    input_data = { 'searchText': username}
    params = { "input" : json.dumps(input_data)}
    async with session.get(base_url, params=params) as response:
        response.raise_for_status()
        data = await response.json()
    api_result = data.get('result', {}).get('data')
    if not api_result or api_result['hasData'] is False:
        return None
    for userId in api_result.get('userIds', []):
        user = await get_user_info(userId, session)
        if username == user['username']:
            PLAYER_CACHE[username] = user['_id']
            return user
    return None

async def get_user_info(userId, session, base_url="https://api2.warera.io/trpc/user.getUserLite"):
    input_data = { 'userId': userId}
    params = { "input" : json.dumps(input_data)}
    async with session.get(base_url, params=params) as response:
        response.raise_for_status()
        data = await response.json()
    api_result = data.get('result', {}).get('data')
    if not api_result:
        return None
    return api_result