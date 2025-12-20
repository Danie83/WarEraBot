import json
import requests

def get_user(username, base_url="https://api2.warera.io/trpc/search.searchAnything"):
    input_data = { 'searchText': username}
    params = { "input" : json.dumps(input_data)}
    response = requests.get(base_url, params=params, headers={
        "Accept": "*/*",
        "Content-Type": "application/json"
    })
    response.raise_for_status()
    api_result = response.json()['result']['data']
    if api_result['hasData'] is False:
        return None
    
    if len(api_result['userIds']) == 0:
        return None
    
    for userId in api_result['userIds']:
        user = get_user_info(userId)
        if username == user['username']:
            return user
    return None

def get_user_info(userId, base_url="https://api2.warera.io/trpc/user.getUserLite"):
    input_data = { 'userId': userId}
    params = { "input" : json.dumps(input_data)}
    response = requests.get(base_url, params=params, headers={
        "Accept": "*/*",
        "Content-Type": "application/json"
    })
    response.raise_for_status()
    api_result = response.json()['result']['data']
    return api_result