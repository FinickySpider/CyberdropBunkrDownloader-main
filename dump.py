import time
import requests
import json
import argparse
import sys
import os
import re
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from tqdm import tqdm
from threading import Thread, Lock, Event
from queue import Queue

# Define global lock for thread safety
lock = Lock()

# Rate limit configuration
INITIAL_DELAY = 1  # Initial delay in seconds between requests
MAX_THREADS = 8  # Maximum number of threads for downloading
RATE_LIMIT_WEIGHT = 0  # Initial weight
MAX_RATE_LIMIT_WEIGHT = 10  # Maximum weight before reducing threads
DECREASE_WEIGHT_RATE = 0.1  # Rate at which the weight decreases over time

# Thread safe variables
current_delay = INITIAL_DELAY
active_threads = MAX_THREADS
stop_event = Event()

# Set to track currently downloading files
currently_downloading = set()

def rate_limited_request(session, url):
    global current_delay
    time.sleep(current_delay)  # Enforce delay between requests
    return session.get(url)

def get_items_list(session, cdn_list, url, retries, extensions, only_export, custom_path=None):
    print(f"[DEBUG] Fetching items list from URL: {url}")
    extensions_list = extensions.split(',') if extensions is not None else []

    r = session.get(url)
    if r.status_code != 200:
        raise Exception(f"[-] HTTP error {r.status_code}")

    soup = BeautifulSoup(r.content, 'html.parser')
    is_bunkr = "| Bunkr" in soup.find('title').text

    direct_link = False

    if is_bunkr:
        print(f"[DEBUG] Detected Bunkr site")
        items = []
        soup = BeautifulSoup(r.content, 'html.parser')

        direct_link = soup.find('a', {'id': 'czmDownloadz'}) is not None or soup.find('div', {'class': 'lightgallery'}) is not None
        if direct_link:
            print(f"[DEBUG] Direct link detected")
            album_name = soup.find('h1', {'class': 'text-[20px]'})
            if album_name is None:
                album_name = soup.find('h1', {'class': 'text-[24px]'})

            album_name = remove_illegal_chars(album_name.text[:album_name.text.index('\n')] if album_name.text.index('\n') > 0 else album_name.text)
            items.append(get_real_download_url(session, cdn_list, url, True))
        else:
            print(f"[DEBUG] Collecting items from grid")
            boxes = soup.find_all('a', {'class': 'grid-images_box-link'})
            for box in boxes:
                items.append({'url': box['href'], 'size': -1})

            album_name = soup.find('h1', {'class': 'text-[24px]'}).text
            album_name = remove_illegal_chars(album_name[:album_name.index('\n')] if album_name.index('\n') > 0 else album_name)
    else:
        print(f"[DEBUG] Detected Cyberdrop site")
        items = []
        items_dom = soup.find_all('a', {'class': 'image'})
        for item_dom in items_dom:
            items.append({'url': f"https://cyberdrop.me{item_dom['href']}", 'size': -1})
        album_name = remove_illegal_chars(soup.find('h1', {'id': 'title'}).text)

    download_path = get_and_prepare_download_path(custom_path, album_name)
    already_downloaded_url = get_already_downloaded_url(download_path)

    real_url_queue = Queue()
    item_queue = Queue()
    for item in items:
        if not direct_link:
            real_url_queue.put((session, cdn_list, item['url'], is_bunkr))
        else:
            item_queue.put((session, item['url'], download_path, is_bunkr, item['name'], retries))

    # Start worker threads to fetch real download URLs
    fetch_threads = []
    for i in range(8):  # Adjust the number of threads based on your needs
        t = Thread(target=fetch_real_download_urls, args=(real_url_queue, item_queue, extensions_list, already_downloaded_url, only_export, download_path, retries))
        t.start()
        fetch_threads.append(t)

    # Wait for all real URL fetch tasks to be completed
    real_url_queue.join()

    # Stop fetch workers
    for i in range(8):
        real_url_queue.put(None)
    for t in fetch_threads:
        t.join()

    # Start worker threads to process the queue
    download_threads = []
    for i in range(MAX_THREADS):  # Adjust the number of threads based on your needs
        t = Thread(target=worker, args=(item_queue,))
        t.start()
        download_threads.append(t)

    # Start thread to manage rate limiting dynamically
    rate_manager_thread = Thread(target=rate_limit_manager, args=(item_queue,))
    rate_manager_thread.start()

    # Wait for all download tasks to be completed
    item_queue.join()

    # Stop download workers
    for i in range(MAX_THREADS):
        item_queue.put(None)
    for t in download_threads:
        t.join()

    # Stop rate limit manager
    stop_event.set()
    rate_manager_thread.join()

    print(f"\t[+] File list exported in {os.path.join(download_path, 'url_list.txt')}" if only_export else f"\t[+] Download completed")
    return

def fetch_real_download_urls(real_url_queue, item_queue, extensions_list, already_downloaded_url, only_export, download_path, retries):
    while True:
        item = real_url_queue.get()
        if item is None:
            break
        session, cdn_list, url, is_bunkr = item
        real_url = get_real_download_url(session, cdn_list, url, is_bunkr)
        if real_url:
            extension = get_url_data(real_url['url'])['extension']
            if (extension in extensions_list or len(extensions_list) == 0) and (real_url['url'] not in already_downloaded_url):
                if only_export:
                    write_url_to_list(real_url['url'], download_path)
                else:
                    item_queue.put((session, real_url['url'], download_path, is_bunkr, real_url.get('name'), retries))
        real_url_queue.task_done()

def get_real_download_url(session, cdn_list, url, is_bunkr=True):
    print(f"[DEBUG] Getting real download URL for: {url}")

    if is_bunkr:
        url = url if 'https' in url else f'https://bunkr.sk{url}'
    else:
        url = url.replace('/f/', '/api/f/')

    r = rate_limited_request(session, url)
    if r.status_code != 200:
        print(f"\t[-] HTTP error {r.status_code} getting real url for {url}")
        return None

    if is_bunkr:
        soup = BeautifulSoup(r.content, 'html.parser')
        source_dom = soup.find('source')
        images_dom = soup.find_all('img')
        links = soup.find_all('a', {'class': 'rounded-[5px]'})

        if source_dom is not None:
            return {'url': source_dom['src'], 'size': -1}
        if images_dom is not None:
            for image_dom in images_dom:
                if image_dom.attrs.get('data-lightbox') is not None:
                    return {'url': image_dom['src'], 'size': -1}
        if links is not None and len(links) > 0:
            url = get_cdn_file_url(session, cdn_list, url)
            return {'url': url, 'size': -1} if url is not None else None
    else:
        item_data = json.loads(r.content)
        return {'url': item_data['url'], 'size': -1, 'name': item_data['name']}

    return None

def get_cdn_file_url(session, cdn_list, gallery_url, file_name=None):
    print(f"[DEBUG] Getting CDN file URL for: {gallery_url}")

    if cdn_list is None:
        print(f"\t[-] CDN list is empty unable to download {gallery_url}")
        return None

    for cdn in cdn_list:
        if file_name is None:
            url_to_test = f"https://{cdn}/{gallery_url[gallery_url.index('/d/')+3:]}"
        else:
            url_to_test = f"https://{cdn}/{file_name}"
        r = rate_limited_request(session, url_to_test)
        if r.status_code == 200:
            return url_to_test
        elif r.status_code == 404:
            continue
        elif r.status_code == 403:
            print(f"\t\t[-] DDoSGuard blocked request to {gallery_url}, skipping")
            return None
        else:
            print(f"\t\t[-] HTTP Error {r.status_code} for {gallery_url}, skipping")
            return None

    return None

def download(session, item_url, download_path, is_bunkr=False, file_name=None, retries=10, backoff_factor=1):
    global RATE_LIMIT_WEIGHT, current_delay, active_threads
    print(f"[DEBUG] Starting download for: {item_url}")

    file_name = get_url_data(item_url)['file_name'] if file_name is None else file_name
    final_path = os.path.join(download_path, file_name)

    file_size = -1  # Initialize file_size
    for attempt in range(1, retries + 1):
        try:
            with session.get(item_url, stream=True, timeout=5) as r:
                if r.status_code == 429:
                    print(f"[-] Error downloading \"{file_name}\": {r.status_code} Too Many Requests")
                    with lock:
                        RATE_LIMIT_WEIGHT += 1
                    time.sleep(backoff_factor * attempt)
                    continue
                if r.status_code != 200:
                    print(f"\t[-] Error downloading \"{file_name}\": {r.status_code}")
                    return
                if r.url == "https://bnkr.b-cdn.net/maintenance.mp4":
                    print(f"\t[-] Error downloading \"{file_name}\": Server is down for maintenance")
                    return

                file_size = int(r.headers.get('content-length', -1))
                with open(final_path, 'wb') as f:
                    with tqdm(total=file_size, unit='iB', unit_scale=True, desc=file_name, leave=False) as pbar:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk is not None:
                                f.write(chunk)
                                pbar.update(len(chunk))
            with lock:
                RATE_LIMIT_WEIGHT = max(0, RATE_LIMIT_WEIGHT - 1)  # Decrease weight after a successful download
                currently_downloading.discard(item_url)  # Mark file as no longer being downloaded
            break
        except requests.exceptions.ConnectionError as e:
            print(f"[DEBUG] ConnectionError during download attempt {attempt} for {item_url}: {e}")
            if attempt < retries:
                time.sleep(2)
            else:
                with lock:
                    currently_downloading.discard(item_url)  # Mark file as no longer being downloaded
                raise e

    if is_bunkr and file_size > -1:
        downloaded_file_size = os.stat(final_path).st_size
        if downloaded_file_size != file_size:
            print(f"\t[-] {file_name} size check failed, file could be broken\n")
            return

    mark_as_downloaded(item_url, download_path)

def rate_limit_manager(queue):
    global current_delay, active_threads
    while not stop_event.is_set():
        with lock:
            if RATE_LIMIT_WEIGHT >= MAX_RATE_LIMIT_WEIGHT:
                current_delay += 0.5  # Increase delay
                active_threads = max(1, active_threads - 1)  # Decrease threads
            elif RATE_LIMIT_WEIGHT == 0:
                current_delay = max(INITIAL_DELAY, current_delay - 0.5)  # Decrease delay
                active_threads = min(MAX_THREADS, active_threads + 1)  # Increase threads
        time.sleep(1)  # Adjust based on the desired frequency of checks

def create_session():
    print(f"[DEBUG] Creating session")
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://bunkr.sk/'
    })
    return session

def get_url_data(url):
    parsed_url = urlparse(url)
    return {'file_name': os.path.basename(parsed_url.path), 'extension': os.path.splitext(parsed_url.path)[1], 'hostname': parsed_url.hostname}

def get_and_prepare_download_path(custom_path, album_name):
    print(f"[DEBUG] Preparing download path for album: {album_name}")

    final_path = 'downloads' if custom_path is None else custom_path
    final_path = os.path.join(final_path, album_name) if album_name is not None else 'downloads'
    final_path = final_path.replace('\n', '')

    if not os.path.isdir(final_path):
        os.makedirs(final_path)

    already_downloaded_path = os.path.join(final_path, 'already_downloaded.txt')
    if not os.path.isfile(already_downloaded_path):
        with open(already_downloaded_path, 'x', encoding='utf-8'):
            pass

    return final_path

def write_url_to_list(item_url, download_path):
    print(f"[DEBUG] Writing URL to list: {item_url}")

    list_path = os.path.join(download_path, 'url_list.txt')

    with open(list_path, 'a', encoding='utf-8') as f:
        f.write(f"{item_url}\n")

    return

def get_already_downloaded_url(download_path):
    print(f"[DEBUG] Getting already downloaded URLs")

    file_path = os.path.join(download_path, 'already_downloaded.txt')

    if not os.path.isfile(file_path):
        return []

    with open(file_path, 'r', encoding='utf-8') as f:
        return f.read().splitlines()

def mark_as_downloaded(item_url, download_path):
    print(f"[DEBUG] Marking URL as downloaded: {item_url}")

    file_path = os.path.join(download_path, 'already_downloaded.txt')
    with open(file_path, 'a', encoding='utf-8') as f:
        f.write(f"{item_url}\n")

    return

def get_cdn_list(session):
    print(f"[DEBUG] Fetching CDN list")
    r = session.get('https://status.bunkr.ru/')
    if r.status_code != 200:
        print(f"[-] HTTP Error {r.status_code} while getting cdn list")
        return None

    cdn_ret = []
    soup = BeautifulSoup(r.content, 'html.parser')
    cdn_list = soup.find_all('h2')
    if cdn_list is not None:
        cdn_list = cdn_list[1:]
        for cdn in cdn_list:
            cdn_ret.append(f"{cdn.text}.bunkr.ru")

    return cdn_ret

def remove_illegal_chars(string):
    return re.sub(r'[<>:"/\\|?*\']|[\0-\31]', "-", string).strip()

def process_url(session, cdn_list, url, retries, extensions, only_export, custom_path=None):
    print(f"[DEBUG] Processing URL: {url}")
    try:
        get_items_list(session, cdn_list, url, retries, extensions, only_export, custom_path)
    except Exception as e:
        print(f"\t[-] Error processing \"{url}\": {e}")

def worker(queue):
    while True:
        item = queue.get()
        if item is None:
            break
        session, item_url, download_path, is_bunkr, file_name, retries = item
        with lock:
            if item_url in currently_downloading:
                queue.task_done()
                continue
            currently_downloading.add(item_url)
        download(session, item_url, download_path, is_bunkr, file_name, retries)
        queue.task_done()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(sys.argv[1:])
    parser.add_argument("-u", help="Url to fetch", type=str, required=False, default=None)
    parser.add_argument("-f", help="File to list of URLs to download", required=False, type=str, default=None)
    parser.add_argument("-r", help="Amount of retries in case the connection fails", type=int, required=False, default=10)
    parser.add_argument("-e", help="Extensions to download (comma separated)", type=str)
    parser.add_argument("-p", help="Path to custom downloads folder")
    parser.add_argument("-w", help="Export url list (ex: for wget)", action="store_true")
    parser.add_argument("-t", help="Number of threads to use for downloading", type=int, required=False, default=4)

    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')

    if args.u is None and args.f is None:
        print("[-] No URL or file provided")
        sys.exit(1)

    if args.u is not None and args.f is not None:
        print("[-] Please provide only one URL or file")
        sys.exit(1)

    session = create_session()
    cdn_list = get_cdn_list(session)

    if args.f is not None:
        with open(args.f, 'r', encoding='utf-8') as f:
            urls = f.read().splitlines()
        for url in urls:
            process_url(session, cdn_list, url, args.r, args.e, args.w, args.p)
    else:
        process_url(session, cdn_list, args.u, args.r, args.e, args.w, args.p)

    print("\t[+] All downloads completed")
    sys.exit(0)
