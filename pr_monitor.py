from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import itertools
import os
import pprint
import requests
import shelve
import threading
import time
from urllib.parse import urlparse, parse_qs

import dateutil.parser


SERVER_PORT = 3030

FILE_PREFIXES = [
]

EXCLUDED_PREFIXES = [
]

_GITHUB_PROJECT_URI = "https://api.github.com/repos/rcahoon/pr_monitor/pulls"

TOKEN = "<TOKEN>"

PR_DB_FILE = os.path.expanduser('~/github-pr-info')
STATE_DB_FILE = os.path.expanduser('~/github-pr-state')

TIME_MIN = datetime.fromtimestamp(0, timezone.utc)


def _get_github_api_headers(token):
    return {
        "Authorization": "token {token}".format(token=token),
        # This header is required to unlock the API used by get_branches_with_head_commit
        "Accept": "application/vnd.github.groot-preview+json",
    }


def _perform_github_api_call(api, data, request_method = requests.post):
    uri = '{uri}{api}'.format(uri=_GITHUB_PROJECT_URI, api=api)
    print(uri, data)
    try:
        request_result = request_method(uri,
                                        headers=_get_github_api_headers(TOKEN),
                                        json=data)
    except requests.exceptions.RequestException as e:
        print("Error requesting {}".format(uri))
        return None

    try:
        output = request_result.json()
    except ValueError:
        print("The response body is not a json: \"{}\"".format(str(request_result)))
        output = {}

    if request_result.status_code > 299:
        print("Github request error code: {}, the response is: {}, the json body is: {}".format(
            str(request_result.status_code), str(request_result), str(output)))
        if 'errors' in list(output.keys()):
            for error in output['errors']:
                print(str(error))
        return None
    elif 200 <= request_result.status_code and request_result.status_code < 300:
        return output
    else:
        print("Unknown error: \nresponse: {} \nbody: {}".format(str(request_result), str(output)))
    return None


def _paginated_github_api_call(api, *args, **kwargs):
    page = 1
    while True:
        query_params = 'page={}'.format(page)
        if '?' not in api:
            query_params = '?' + query_params
        elif api[-1] != '&':
            query_params = '&' + query_params

        output = _perform_github_api_call(api + query_params, *args, **kwargs)
        if output is None:
            return None

        if len(output) == 0:
            break

        assert isinstance(output, list)
        yield from output
        page += 1


def list_pull_requests(only_open=False):
    api = "pulls?sort=updated&direction=desc"
    if only_open:
        api += "&state=open"
    else:
        api += "&state=all"
    return _paginated_github_api_call(
        api,
        None,
        request_method=requests.get,
    )


def get_pull_request_files(pull_number):
    return _paginated_github_api_call("pulls/{}/files".format(pull_number),
                                      None, request_method=requests.get)


def get_pull_request_filenames(pull_number):
    response = get_pull_request_files(pull_number)
    if response is None:
        return None
    return [label["filename"] for label in response]


def run_server(db, user_db, db_lock):
    # We use a class factory function because there's not an easy way to pass
    # arguments to a BaseHTTPRequestHandler class - so instead we capture them
    # in the local closure of the class.

    class Server(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            if query.get('operation') == ['read']:
                with db_lock:
                    key = query['pr'][-1]
                    visited_at = datetime.fromtimestamp(float(query['time'][-1]), timezone.utc)
                    print('Setting', key, 'visited time to', visited_at)
                    pr_state = user_db.get(key, {})
                    pr_state['visited'] = visited_at
                    user_db[key] = pr_state

                self.send_response(303)
                self.send_header('Location', '/')
                self.end_headers()
                return

            list_items = []
            with db_lock:
                for key, value in db.items():
                    if key.startswith('_'):
                        continue
                    matching_files = [
                        f
                        for f in value['files']
                        if any(f.startswith(prefix) for prefix in FILE_PREFIXES)
                        and not any(f.startswith(prefix) for prefix in EXCLUDED_PREFIXES)
                    ]
                    if not matching_files:
                        continue

                    item_content = '<details><summary>'

                    item_title = '{} {}'.format(key, value['title'])
                    if user_db.get(key, {}).get('visited', TIME_MIN) < value['updated']:
                        item_title = '<b>{}</b>'.format(item_title)
                    elif value.get('state') == 'closed':
                        continue
                    else:
                        item_title = '<i>{}</i>'.format(item_title)
                    item_content += '<a href="{}">{}</a>'.format(value['url'], item_title)

                    item_content += ' <a href="?operation=read&pr={}&time={}">read</a>'.format(key, value['updated'].replace(tzinfo=timezone.utc).timestamp())
                    item_content += ' <a href="?operation=read&pr={}&time=0">unread</a>'.format(key)

                    item_content += '</summary><ul>'
                    item_content += '<pre>{}</pre>'.format(value.get('description', ''))
                    for f in matching_files:
                        item_content += "<li>{}</li>".format(f)
                    item_content += "</ul></details>"
                    list_items.append((value['updated'], item_content))

            list_items.sort(reverse=True)
            content = '<html><head><meta charset="utf-8" /><meta http-equiv="refresh" content="30" /></head><body>'
            content += """
                <style>
                body {
                  padding-left: 20px;
                  padding-right: 200px;
                }
                pre {
                  white-space: pre-wrap;
                }
                a {
                  color: blue;
                  text-decoration: none;
                }
                a i {
                  text-decoration: underline;
                }
                a b {
                  text-decoration: underline;
                }
                </style>
            """
            content += "".join(li[1] for li in list_items)
            content += "</body></html>"

            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(bytes(content, "utf-8"))

    web_server = HTTPServer(("0.0.0.0", SERVER_PORT), Server)
    print("Serving on {}:{}".format("0.0.0.0", SERVER_PORT))
    try:
        web_server.serve_forever()
    finally:
        web_server.server_close()


def main():
    db_lock = threading.Lock()
    with shelve.open(PR_DB_FILE, flag='c') as db, \
            shelve.open(STATE_DB_FILE, flag='c') as user_db:
        server_thread = threading.Thread(target=run_server, args=(db, user_db, db_lock))
        server_thread.daemon = True
        server_thread.start()

        while True:
            with db_lock:
                last_update = db.get('_last_update')
            if last_update is None:
                new_prs = list_pull_requests(only_open=True)
            else:
                new_prs = itertools.takewhile(
                    lambda pr: dateutil.parser.parse(pr['updated_at']) > last_update,
                    list_pull_requests(only_open=False),
                )
            new_last_update = None
            for pr in new_prs:
                updated_at = dateutil.parser.parse(pr['updated_at'])
                print("PR", pr['number'], updated_at)
                if new_last_update is None or updated_at > new_last_update:
                    new_last_update = updated_at
                pr_files = get_pull_request_filenames(pr['number'])
                with db_lock:
                    db['pr{}'.format(pr['number'])] = {
                        'url': pr['html_url'],
                        'title': pr['title'],
                        'description': pr['body'],
                        'updated': updated_at,
                        'files': pr_files,
                        'state': pr['state'],
                    }
            if new_last_update is not None:
                with db_lock:
                    db['_last_update'] = new_last_update
                    db.sync()
                print(datetime.now(), 'Done', new_last_update.astimezone())
            else:
                print(datetime.now(), 'Done', 'No updates')
            print('\n\n')

            '''
            with db_lock:
                for key, value in db.items():
                    if key.startswith('_'):
                        continue
                    matching_files = [
                        f
                        for f in value['files']
                        for prefix in FILE_PREFIXES
                        if f.startswith(prefix)
                    ]
                    if not matching_files:
                        continue
                    print(key, value['title'])
                    for f in matching_files:
                            print('  ', f)
                print('\n\n')
            '''

            time.sleep(600)


if __name__ == "__main__":
    main()
