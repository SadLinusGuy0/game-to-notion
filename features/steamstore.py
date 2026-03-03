from bs4 import BeautifulSoup
from urllib import request
from http import cookiejar


def get_steam_store_info(appid):
    url = f"https://store.steampowered.com/app/{appid}/?l=english"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }

    # Set cookies to bypass age verification
    cj = cookiejar.CookieJar()
    opener = request.build_opener(request.HTTPCookieProcessor(cj))
    request.install_opener(opener)

    cookies = {
        'birthtime': '568022401',
        'lastagecheckage': '1-January-1990',
        'wants_mature_content': '1'
    }
    headers['Cookie'] = "; ".join([f"{k}={v}" for k, v in cookies.items()])

    req = request.Request(url, headers=headers)
    metainfo = {'info': '', 'tag': []}

    try:
        with request.urlopen(req, timeout=10) as response:
            html = response.read().decode('utf-8')
    except Exception as e:
        print(f"Request failed: AppID {appid}, error: {e}")
        return metainfo

    soup = BeautifulSoup(html, 'html.parser')

    # Description
    try:
        info_elements = soup.find_all('div', {'class': 'game_description_snippet'})
        if info_elements:
            metainfo['info'] = info_elements[0].get_text(strip=True)
    except Exception as e:
        print(f"Description extraction failed: AppID {appid}, error: {e}")
        return metainfo

    # Tags
    try:
        tag_container = soup.find_all('a', {'class': 'app_tag'})
        tags = [tag.get_text(strip=True) for tag in tag_container if tag.get_text(strip=True)]
        metainfo['tag'] = [{'name': tag} for tag in tags]
    except Exception as e:
        print(f"Tag extraction failed: AppID {appid}, error: {e}")
        return metainfo

    return metainfo
