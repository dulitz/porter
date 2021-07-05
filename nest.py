"""
nest.py

The Nest module for porter, the Prometheus exporter.

Uses the (new) Google Smart Device Management API.

See https://developers.google.com/nest/device-access/api/thermostat
"""

import json, prometheus_client, requests, time, threading
#from dateutil.parser import isoparse
from prometheus_client.core import GaugeMetricFamily


REQUEST_TIME = prometheus_client.Summary('nest_processing_seconds',
                                         'time of nest requests')

class NestError(Exception):
    pass

class NestClient:
    OAUTH_PREFIX = 'https://www.googleapis.com/oauth2/v4/token?grant_type=refresh_token'
    API_PREFIX = 'https://smartdevicemanagement.googleapis.com/v1/enterprises/'

    def __init__(self, config):
        self.config = config
        self.cv = threading.Condition()
        self.accesstokens = {}
        nconfig = config.get('nest')
        if not nconfig:
            raise Exception('no nest configuration')
        if not nconfig.get('projectid'):
            raise Exception('no nest projectid')
        if not nconfig.get('clientid'):
            raise Exception('no nest clientid')
        if not nconfig.get('clientsecret'):
            raise Exception('no nest clientsecret')
        if not nconfig.get('credentials'):
            raise Exception('no nest credentials')
        if not nconfig.get('timeout'):
            nconfig['timeout'] = 10

    def _fetch_token_json(self, user):
        # holding self.cv
        nconfig = self.config['nest']
        timeout = nconfig['timeout']
        refresh_token = nconfig['credentials'].get(user)
        if refresh_token is None:
            raise NestError(f'no credentials for {user}')
        resp = requests.post(f'{self.OAUTH_PREFIX}&client_id={nconfig["clientid"]}&client_secret={nconfig["clientsecret"]}&refresh_token={refresh_token}', timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def get_access_token(self, user):
        with self.cv:
            d = self.accesstokens.get(user, {})
            refetch_at = d.get('expires_in', 0)/2 + d.get('fetched_at', 0)
            now = time.time()
            if refetch_at <= now:
                newd = self._fetch_token_json(user)
                if newd:
                    newd['fetched_at'] = now
                    self.accesstokens[user] = newd
                    d = newd
            if not d:
                raise NestError(f'could not refresh {user}')
            return d['access_token']

    def bearer_json_request(self, access_token, command, path, data=None):
        # e.g. path is /devices
        endpoint = f'{self.API_PREFIX}{self.config["nest"]["projectid"]}{path}'
        headers = { 'Content-Type': 'application/json',
                    'Authorization': f'Bearer {access_token}' }
        timeout = self.config['nest']['timeout']
        if data: # depending on command, data may not be allowed as an argument
            resp = command(endpoint, headers=headers, timeout=timeout, data=data)
        else:
            resp = command(endpoint, headers=headers, timeout=timeout)
        resp.raise_for_status()
        if resp.status_code == 204:
            return None
        return resp.json()

    @REQUEST_TIME.time()
    def collect(self, target):
        """get the status of all devices"""

        metric_to_gauge = {}
        def makegauge(metric, desc, morelabels=[]):
            already = metric_to_gauge.get(metric)
            if already:
                return already
            labels = ['deviceId', 'nameLabel', 'location', 'room'] + morelabels
            gmf = GaugeMetricFamily(metric, desc, labels=labels)
            metric_to_gauge[metric] = gmf
            return gmf

        token = self.get_access_token(target)
        resp = self.bearer_json_request(token, requests.get, '/devices')
        for device in resp.get('devices', []):
            deviceid = device['name'].split('/')[-1] # last path component is the deviceid
            basetype = device['type'].split('.')[-1].lower() # e.g. 'thermostat'
            customname = device.get('traits', {}).get('sdm.devices.traits.Info', {}).get('customName', '')
            location, roomname = '', ''
            for d in device.get('parentRelations', []):
                p = d.get('parent')
                if p and not roomname:
                    # e.g. enterprises/88f863/structures/W6hYBQCS/rooms/PPrzWmR99RRz
                    path = p.split('/')
                    location = '/'.join(path[2:4])
                    roomname = d.get('displayName')
            labelvalues = [deviceid, customname, location, roomname]

            for (n, d) in device.get('traits', {}).items():
                for (nn, vv) in d.items():
                    if nn == 'ambientHumidityPercent':
                        g = makegauge('humidity_pct', 'percent ambient humidity')
                        g.add_metric(labelvalues, vv)
                    elif nn == 'ambientTemperatureCelsius':
                        g = makegauge('temp_c', 'ambient temperature (degrees Celsius)')
                        g.add_metric(labelvalues, vv)
                    elif n == 'sdm.devices.traits.Connectivity' and nn == 'status':
                        online = (vv.lower() == 'online')
                        g = makegauge('is_online', 'true if is online',
                                      morelabels=['state'])
                        g.add_metric(labelvalues + [vv], online)
                    elif n == 'sdm.devices.traits.Fan' and nn == 'timerMode':
                        on = (vv.lower() != 'off')
                        g = makegauge('fan_manual_on', 'true if fan is manually on',
                                      morelabels=['state'])
                        g.add_metric(labelvalues + [vv], on)
                    elif n == 'sdm.devices.traits.ThermostatMode' and nn == 'mode':
                        def makeit(mode):
                            g = makegauge(f'thermostat_mode_is_{mode.lower()}',
                                          f'true if thermostat mode is {mode.upper()}',
                                          morelabels=['state'])
                            g.add_metric(labelvalues + [vv], vv.lower() == mode.lower())
                        makeit('heat')
                        makeit('cool')
                        makeit('heatcool')
                        makeit('off')
                    elif n == 'sdm.devices.traits.ThermostatEco' and nn == 'mode':
                        on = (vv.lower() != 'off')
                        g = makegauge('thermostat_eco_on', 'true if in manual eco mode',
                                      morelabels=['state'])
                        g.add_metric(labelvalues + [vv], on)
                    elif n == 'sdm.devices.traits.ThermostatHvac' and nn == 'status':
                        def makeit(state):
                            g = makegauge(f'thermostat_state_is_{state.lower()}',
                                          f'true if thermostat state is {state.upper()}',
                                          morelabels=['state'])
                            g.add_metric(labelvalues + [vv], vv.lower() == state.lower())
                        makeit('heating')
                        makeit('cooling')
                    elif n == 'sdm.devices.traits.ThemostatTemperatureSetpoint':
                        if nn == 'heatCelsius':
                            gheat = makegauge('thermostat_heat_setpoint_c',
                                              'thermostat heat setpoint (deg Celsius)')
                            gheat.add_metric(labelvalues, vv)
                        elif nn == 'coolCelsius':
                            gcool = makegauge('thermostat_cool_setpoint_c',
                                              'thermostat cool setpoint (deg Celsius)')
                            gcool.add_metric(labelvalues, vv)

        return metric_to_gauge.values()

def prompt_for_project():
    print('''
To create Google developer credentials, follow these steps:

STEP 1. Create a new project in the Google Cloud Platform Console

In your browser, visit https://console.cloud.google.com/projectcreate
''')
    input('Press ENTER when complete: ')
    print('''
STEP 2. Create OAuth credentials in the GCP Console

In your browser, visit https://console.cloud.google.com/apis/credentials
Create an OAuth 2.0 client ID **for desktop** and download the credentials.json
''')
    while True:
        credfile = input('Enter the path to the credentials.json file: ')
        creds = {}
        try:
            with open(credfile, 'rt') as c:
                creds = json.load(c)
                break
        except IOError as e:
            print(e)
            continue
        except OSError as e:
            print(e)
            continue
    clientid = creds.get('installed', {}).get('client_id')
    clientsecret = creds.get('installed', {}).get('client_secret')
    if clientid and clientsecret:
        pass
    else:
        print('could not load credentials -- exiting')
        return

    ##### may need to set up OAuth consent screen in GCP Console
    ##### may need to add yourself as a test user

    print('''
STEP 3. Enable the Smart Device Management API in the GCP Console

In your browser, visit https://console.cloud.google.com/apis/library/smartdevicemanagement.googleapis.com
''')
    input('Press ENTER when complete: ')
    print(f'''
STEP 4. Create a project in the Google Nest Device Access Console

This requires you to connect a credit card and pay $5.

In your browser, visit https://console.nest.google.com/device-access/project-list
  - Enter a name such as "Porter for Prometheus"
  - When it asks for the OAuth Client ID, paste this:
         {clientid}
  - Disable events (Pub/Sub) for now.
''')
    projectid = input('When complete, copy the project ID and enter it here: ')
    uri = f'https://nestservices.google.com/partnerconnections/{projectid}/auth?redirect_uri=https://localhost&access_type=offline&prompt=consent&client_id={clientid}&response_type=code&scope=https://www.googleapis.com/auth/sdm.service'
    print(f'''
STEP 5. Obtain an OAuth code

In your browser, visit {uri}
  - Agree to the consent screens.
  - The final page will fail to load and that is okay.
''')
    (resp, refreshtoken) = get_refresh(clientid, clientsecret, projectid)
    print(json.dumps(resp.json(), indent=2))
    print(f'''
Add these lines to your porter.yml file:

nest:
  projectid: {projectid}
  clientid: {clientid}
  clientsecret: {clientsecret}
  credentials:
    'YOUR_TARGETNAME_GOES_HERE': '{refreshtoken}'
''')

def get_refresh(clientid, clientsecret, projectid):
    while True:
        finaluri = input('Paste the URL from the browser bar after you have agreed to the consent screens: ')
        (junk, mid, end) = finaluri.partition('code=')
        if end:
            (code, amp, end) = end.partition('&')
            break
        print('The URL you entered does not contain a code. Please try again.')

    resp = requests.post(f'https://www.googleapis.com/oauth2/v4/token?client_id={clientid}&client_secret={clientsecret}&code={code}&grant_type=authorization_code&redirect_uri=https://localhost')
    resp.raise_for_status()
    c = resp.json()
    accesstoken = c.get('access_token')
    refreshtoken = c.get('refresh_token')
    endpoint = f'https://smartdevicemanagement.googleapis.com/v1/enterprises/{projectid}/devices'
    headers = { 'Content-Type': 'application/json',
                'Authorization': f'Bearer {accesstoken}' }
    resp = requests.get(endpoint, headers=headers, timeout=20)
    resp.raise_for_status()
    return (resp, refreshtoken)

def prompt_for_refresh(config):
    clientid = config['nest']['clientid']
    clientsecret = config['nest']['clientsecret']
    projectid = config['nest']['projectid']
    uri = f'https://nestservices.google.com/partnerconnections/{projectid}/auth?redirect_uri=https://localhost&access_type=offline&prompt=consent&client_id={clientid}&response_type=code&scope=https://www.googleapis.com/auth/sdm.service'
    print(f'''
In your browser, visit {uri}
  - Agree to the consent screens.
  - The final page will fail to load and that is okay.
''')
    (resp, refreshtoken) = get_refresh(clientid, clientsecret, projectid)
    print(f'''
  credentials:
    'YOUR_TARGETNAME_GOES_HERE': '{refreshtoken}'
''')
    

if __name__ == '__main__':
    import sys, yaml
    assert len(sys.argv) == 2, sys.argv
    config = yaml.safe_load(open(sys.argv[1]))
    try:
        creds = config.get('nest', {}).get('credentials', {})
        if creds:
            client = NestClient(config)
            for (target, refresh) in creds.items():
                token = client.get_access_token(target)
                resp = client.bearer_json_request(token, requests.get, '/devices')
                print(f'***************** {target}')
                print(json.dumps(resp, indent=2))
        else:
            print('no nest credentials in config')
            prompt_for_project()
    except NestError as e:
        print(f'could not instantiate client: {e}')
        prompt_for_project()
    except requests.exceptions.HTTPError as e:
        print(f'error instantiating client: {e}')
        prompt_for_refresh(config)
