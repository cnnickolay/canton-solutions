"""Minimal client for the Canton JSON Ledger API v2."""
import json
import urllib.error
import urllib.request

import websocket


class ApiError(RuntimeError):
    """A non-2xx answer from the JSON API. `body` is the decoded error."""

    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(json.dumps(body))


class JsonApi:
    def __init__(self, url, token=None):
        self.url = url.rstrip('/')
        self.headers = {'Authorization': f'Bearer {token}'} if token else {}

    def post(self, path, body=None):
        """POST `body` as JSON, or GET when there is no body."""
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.url + path, data, {'Content-Type': 'application/json', **self.headers})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raw = error.read().decode()
            try:
                decoded = json.loads(raw)
            except ValueError:
                decoded = {'message': raw}
            raise ApiError(error.code, decoded) from error
        return json.loads(raw) if raw else {}

    def socket(self, path, body, timeout=30):
        """Open a websocket endpoint and send the request body."""
        url = self.url.replace('https:', 'wss:').replace('http:', 'ws:')
        connection = websocket.create_connection(url + path, header=self.headers, timeout=timeout)
        connection.send(json.dumps(body))
        return connection

    def offset(self):
        return self.post('/v2/state/ledger-end')['offset']

    def contracts(self, parties, offset=None):
        """Active contracts visible to `parties` at one offset. Uses the stream,
        because the plain HTTP endpoint stops at 200 results."""
        wildcard = {'identifierFilter': {'WildcardFilter': {'value': {'includeCreatedEventBlob': True}}}}
        body = {
            'activeAtOffset': self.offset() if offset is None else offset,
            'filter': {'filtersByParty': {party: {'cumulative': [wildcard]} for party in parties}},
            'verbose': True,
        }
        connection = self.socket('/v2/state/active-contracts', body)
        found = {}
        try:
            while raw := connection.recv():
                row = json.loads(raw)
                if 'contractEntry' not in row:
                    raise RuntimeError(f'Unexpected active contracts row: {row}')
                active = row['contractEntry'].get('JsActiveContract')
                if active:
                    event = active['createdEvent']
                    found[event['contractId']] = event
        finally:
            connection.close()
        return list(found.values())


def exercise_cmd(template, cid, choice, argument):
    return {'ExerciseCommand': {'templateId': template, 'contractId': cid,
                                'choice': choice, 'choiceArgument': argument}}


def create_cmd(template, argument):
    return {'CreateCommand': {'templateId': template, 'createArguments': argument}}
