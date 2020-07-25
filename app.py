#!/usr/bin/env python3

from flask import Flask, request, Response
from datetime import datetime, timedelta
import urllib.parse as urlparse
import json
import hashlib
import requests
import logging
import re
import sys
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

app = Flask(__name__)

####
# Basic configuration - Update the following to match your environment
SUBSCRIPTION_CALLBACK_URL = 'https://vs.breker.name'

####
# Advanced Configuration
SUBSCRIPTION_CALLBACK_PATH = 'handle_subscription_callback'
GRAPH_API_URL = 'https://graph.microsoft.com/'
# Resubscribe and do a full calendar sync every 23h
HOURS_BETWEEN_FULLSYNC_AND_RESCUBSCRIBE = 23
# Make subscriptions last for 24h
HOURS_TO_SUBSCRIBE = 24
# Get upto the next 8 days, so we always have 7 days in the cache
CALENDAR_DAYS_TO_CACHE = 8


# ToDo: store in redis or a credential wallet.
globalstate = {}


class MiddlewareException(Exception):
    def __init__(self, msg=''):
        self.msg = msg
        logging.error(msg)


def store_data(hkey, state):
    global globalstate
    # ToDo: need to wipe old entries on some point, as it will eventually run out of memory
    globalstate[hkey] = state


def get_data(hkey):
    return globalstate[hkey]


@app.route("/trigger_middleware")
def trigger_middleware():
    """ Main entry point - gets regularly triggered by a data endpoint
        configured in the integration. This is where the magic starts.  """
    # We trust the webhook URLs to form a unique key
    s = (request.args['calendar_webhook']
         + request.args['email_webhook']).encode('utf-8')
    _hkey = hashlib.sha512(s).hexdigest()
    # We split the hkey, so it's not completely contained in the url, that may get logged
    # Yet, we can't put it all into the clientstate, as the SoR restrict the clientstate length.
    hkey = [_hkey[0:32], _hkey[32:]]
    # Check whether it's time for a sync
    try:
        state = get_data(_hkey)
        next_sync = state['next_sync']
    except KeyError:
        next_sync = 0
    authorization = request.headers.get('Authorization')
    calendar_webhook = request.args.get('calendar_webhook')
    email_webhook = request.args.get('email_webhook')
    if next_sync == 0 or datetime.now() >= next_sync:
        # Need to resync
        headers = get_headers(authorization)
        users = get_all_users(headers)
        # We pull the calendar regularly, to avoid having to cache all times,
        # and to be able to report events planed before the webhook got
        # configured.
        update_calendar(authorization, users, calendar_webhook)
        wipe_subscriptions(authorization)
        register_subscriptions(authorization, hkey, users,
                               calendar_webhook,
                               email_webhook)
        next_sync = datetime.now()+timedelta(hours=HOURS_BETWEEN_FULLSYNC_AND_RESCUBSCRIBE)
    # We store the SoR bearer token, to make APi calls with it later
    state = {'authorization': authorization,
             'calendar_webhook': calendar_webhook,
             'email_webhook': email_webhook,
             'next_sync': next_sync}
    store_data(_hkey, state)

    return Response('{"status": "ok"}', status=200)


def wipe_subscriptions(authorization):
    """ Wipe pre-existing event subscriptions, to start with a clean slate,
        and to avoid being notified twice. """
    headers = get_headers(authorization)
    subscriptions = odata_get(f"{GRAPH_API_URL}/v1.0/subscriptions/?"
                              + "select=id,notificationUrl",
                              headers=headers)
    if subscriptions is None:
        raise MiddlewareException("Failed to wipe subscriptions")
    for subscription in subscriptions:
        # ToDo: could probably do this using a filter as part of the request
        if not subscription['notificationUrl'].startswith(SUBSCRIPTION_CALLBACK_URL):
            continue
        r = requests.delete(f"{GRAPH_API_URL}/v1.0/subscriptions/"
                            + f"{subscription['id']}",
                            headers=headers)
        if not r.ok:
            raise MiddlewareException("Failed to delete subscription "
                                      + f"{subscription['id']} due to "
                                      + f"{r.status_code}")


def register_subscriptions(authorization, hkey, users, calendar_webhook, email_webhook):
    """ Register for change events in emails (messages) and
        calendar (events) """
    headers = get_headers(authorization)
    future = datetime.now()+timedelta(hours=HOURS_TO_SUBSCRIBE)
    untilstring = future.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")

    for user in users:
        for subscription in ['messages', 'events']:
            data = {
                "changeType": "created,updated,deleted",
                "notificationUrl": (SUBSCRIPTION_CALLBACK_URL + "/" +
                                    SUBSCRIPTION_CALLBACK_PATH + "/" +
                                    subscription + "/" + hkey[1]),
                "resource": f"/users/{user['id']}/{subscription}/",
                "expirationDateTime": untilstring,
                # Store some metadata in the clientState, to be able to
                # asociate the request later.
                "clientState": (hkey[0] + "^" +
                                user['mail']+"^"+user['manager_mail']),
            }
            r = requests.post(f"{GRAPH_API_URL}/v1.0/subscriptions",
                              headers=headers, data=json.dumps(data))
            if not r.ok:
                logging.warning(f"Unable to subscribe {user['mail']} for "
                                f"{subscription}: {r.text}")
            else:
                logging.debug(f"Subscribed {user['mail']} to {subscription}")


@app.route("/"+SUBSCRIPTION_CALLBACK_PATH+"/<subscription>/<hkey1>", methods=['POST'])
def handle_subscription_callback(subscription, hkey1):
    """ This is where we get event callbacks from O365 """
    # Do the O365 webhook validation dance
    if 'validationToken' in request.args:
        logging.debug("Returning validationToken")
        return Response(request.args['validationToken'], status=200,
                        mimetype='text/plain; charset=utf-8')

    jsonbody = json.loads(request.data)
    # More than one event can come in a single callback
    for value in jsonbody['value']:
        try:
            _handle_subscription_callback_value(subscription, hkey1, value)
        except MiddlewareException:
            # This is a rather common path, e.g. during event deletion,
            # there is an update followed directly by delete, so we'd
            # race trying to get the update.
            pass

    # O365 expects a 202 return code and will otherwise keep resending.
    return Response('', status=202)


def _handle_subscription_callback_value(subscription, hkey1, value):
    # Decode the metadata which we placed before
    client_state = value['clientState']
    (hkey0, mail, manager_mail) = client_state.split('^')
    # hleu is split between url and client_state - put it back together
    hkey = hkey0+hkey1

    try:
        globalstateentry = get_data(hkey)
    except KeyError:
        logging.warning("Received unexpected hkey %s" %
                        (hkey))
        # Still return to 202 to avoid O365 hitting us with the same callback
        return Response('', status=202)

    odata_id = value['resourceData']['@odata.id']
    shortid = odata_id.rsplit('/', 1)[1]

    if subscription == 'messages':
        if value['changeType'] == 'deleted':
            # Message deleted, so we remove the cache entry
            requests.delete(
                f"{globalstateentry['email_webhook']}/?id={shortid}")
        else:
            # Message new or changed, so we update the cache
            process_message(globalstateentry, mail, manager_mail, odata_id)
    elif subscription == 'events':
        if value['changeType'] == 'deleted':
            # Event deleted, so we remove the cache entry
            requests.delete(
                f"{globalstateentry['calendar_webhook']}/?id={shortid}")
        else:
            # Event changed, so we update the cache
            process_event(globalstateentry, mail, odata_id)
    else:
        raise MiddlewareException("Unknown subscription type")


def process_message(globalstateentry, mail, manager_mail, odata_id):
    # Handle email
    # First of all - we need to get the real data
    headers = get_headers(globalstateentry['authorization'])
    jsondata = odata_getone(f"{GRAPH_API_URL}/v1.0/{odata_id}?"
                            + "$select=id,subject,from,toRecipients,"
                            + "importance,sentDateTime,webLink,isRead",
                            headers=headers)
    if not jsondata:
        raise MiddlewareException(
            f"Failed to resolve message in process_message {odata_id}")
    is_from_manager = (
        manager_mail == jsondata['from']['emailAddress']['address'].lower())
    # Filter the data to avoid spaming the cache
    if not is_from_manager and jsondata['importance'] != 'high':
        return
    jsondata.update({'owner': mail,
                     'is_from_manager': is_from_manager})
    logging.debug(
        f"Inserting email {jsondata['id']} into cache via webhook")
    # Todo: Workaround for unreliable pushes
    for i in range(0, 5):
        r = requests.put(
            globalstateentry['email_webhook'], data=json.dumps(jsondata))
    if not r.ok:
        logging.warning(
            f"Failed to put to {globalstateentry['email_webhook']}: {r.text}")


def process_event(globalstateentry, mail, odata_id):
    # Handle calendar event
    # First of all - we need to get the real data
    headers = get_headers(globalstateentry['authorization'])
    jsondata = odata_getone(f"{GRAPH_API_URL}/v1.0/{odata_id}?"
                            + "$select=id,subject,location,organizer,start,"
                            + "end,weblink,responsestatus,body,attendees,"
                            + "isCancelled",
                            headers=headers)
    if not jsondata:
        raise MiddlewareException(
            f"Failed to resolve event in process_event {odata_id}")
    parse_event(jsondata, mail, globalstateentry['calendar_webhook'])


def update_calendar(authorization, users, calendar_webhook):
    # Read a user's calendar, as relying on webhooks alone wouldn't reveal
    # entries pre-existing prior to subscribing to webhooks.
    startdatetime = datetime.now()
    startdatetime_iso = startdatetime.isoformat()
    enddatetime = startdatetime + timedelta(days=CALENDAR_DAYS_TO_CACHE)
    enddatetime_iso = enddatetime.isoformat()
    headers = get_headers(authorization)
    for user in users:
        events = odata_get("https://graph.microsoft.com/v1.0/"
                           + f"users/{user['id']}/calendarview?"
                           + "startdatetime="+startdatetime_iso
                           + "&enddatetime="+enddatetime_iso
                           + "&select=id,subject,location,organizer,start,"
                           + "end,weblink,responsestatus,body,attendees,"
                           + "isCancelled",
                           headers=headers)
        if events is None:
            logging.error("Failed to get events user user {user['id']}")
            continue
        logging.warning('Got %d events' % (len(events)))
        for event in events:
            parse_event(event, user['mail'], calendar_webhook)


def parse_event(event, owner, calendar_webhook):
    # Filter the data, to avoid spaming the cache
    eventdt = datetime.strptime(event['start']['dateTime'].split('.', 1)[0],
                                '%Y-%m-%dT%H:%M:%S')
    if (eventdt < datetime.now() - timedelta(days=1)
            or eventdt > datetime.now() + timedelta(days=CALENDAR_DAYS_TO_CACHE)):
        return

    # ToDo: could probably just pass-through the event - but would need
    # to rebuild apps for that
    meeting_link = extract_meetinglink(
        event['location']['displayName']+'^'+event['body']['content'])
    event.update({'owner': owner,
                  'meetingLink': meeting_link,
                  # Workaround: Doesn't currently seem possible to delete via
                  # webhooks, unless a single primarykey field is used
                  #'oneprimarykey': f"{owner}^{event['id']}"}
                 })
    logging.debug(f"Inserting event {event['id']} into cache via webhook")
    # Todo: Workaround for unreliable pushes
    for i in range(0, 5):
        r = requests.put(calendar_webhook, data=json.dumps(event))
    if not r.ok:
        logging.warning(f"Failed to put to {calendar_webhook}: {r.text}")


""" Pass-through other requests to the SoR, MS Graph """
@app.route("/", defaults={"path": ""}, methods=['GET', 'POST', 'DELETE'])
@app.route("/<path:path>", methods=['GET', 'POST', 'DELETE'])
def pass_through(path):
    pathsplit = path.split('/', 1)
    if ((len(pathsplit) < 2
         or pathsplit[0] not in ['v1.0', 'beta']
         or request.headers.get('Authorization') == None)):
        logging.warning(f"Ignoring unknown forwarding request for {path}")
        return Response('', status=503)
    r = requests.request(request.method, f'{GRAPH_API_URL}/{path}', data=request.data,
                         params=request.args, headers=request.headers)
    if not r.ok or r.status_code < 200 or r.status_code > 299:
        logging.warning(f"Failed pass-through of {request.method} to {path} with {r.status_code} "
                        + f"due to {r.text}")
    return Response(r.content, status=r.status_code)

###################
# Utility functions
###################


def get_all_users(headers):
    users = odata_get(f"{GRAPH_API_URL}/v1.0/users",
                      headers=headers)
    if users is None:
        raise MiddlewareException("Failed to get users")

    # ToDo: Finding every users manager with a separate API call, sounds
    # expensive. Is there a better way?
    for idx, user in enumerate(users):
        manager = odata_getone(f"{GRAPH_API_URL}/v1.0/users/{user['id']}/"
                               + "manager?$select=mail",
                               headers=headers)
        if manager and 'mail' in manager:
            users[idx]['manager_mail'] = manager['mail'].lower()
        else:
            users[idx]['manager_mail'] = ''
    return users


def odata_get(url, headers):
    # Get a list from odata. Follow nextLink where needed.
    all_values = list()
    while url:
        rjson = odata_getone(url, headers)
        url = None
        if rjson:
            all_values.extend(rjson['value'])
            try:
                url = rjson['@odata.nextLink']
            except KeyError:
                pass
    return all_values


def odata_getone(url, headers):
    # Get a single object from Odata
    logging.debug(f"Fetching data from {url}")
    r = requests.get(url, headers=headers)
    if not r.ok:
        logging.warning(f"Fetch url {url} hit {r.status_code}")
        return None
    rjson = r.json()
    if 'error' in rjson:
        logging.warning(f"Fetching of {url} returned error {r.text}")
        return None
    return rjson


def extract_meetinglink(astring):
    meetinglink = None
    urls = re.findall(r'(https://\S+)', astring)
    for url in urls:
        urlp = urlparse.urlparse(url)
        if ((urlp.netloc in ["gotomeet.me", "www.gotomeet.me", "global.gotomeeting.com", "teams.microsoft.com"]
             or urlp.netloc.endswith(".webex.com"))):
            meetinglink = url
            break
    return meetinglink


def get_headers(authorization):
    return {
        'Authorization': authorization,
        'Content-type': 'application/json'
    }


if __name__ == "__main__":
    app.run(host='127.0.0.1', port=1340)