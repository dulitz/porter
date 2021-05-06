# neurio.py
#
# the Neurio (Generac PWRview) module for porter, the Prometheus exporter
#
# see https://api-docs.neur.io/#
# and to register an app: https://mypwrview.generac.com/#settings/applications/


import requests, prometheus_client, time

from prometheus_client.core import GaugeMetricFamily

REQUEST_TIME = prometheus_client.Summary('neurio_processing_seconds',
                                         'time of neurio requests')

#ENDPOINT = 'https://api.neur.io/v1/'
#
# 
# to get Bearer token for personal data, see https://api-docs.neur.io/#oauth-2.0-token-post
#
# POST https://api.neur.io/v1/oauth2/token
# with data:
#   grant_type=client_credentials&client_id=BVlMq9azS7abZyq2wc1gnA&client_secret=XXXXrrXXXXzZZZoZZZZ
# result is a dictionary with keys access_token, created_at, and expires_in

#def get_token_by_exchanging_for_code(self, code):
#    oauth = OAuth2Session(self._get_clientid(), redirect_uri=self._get_redirecturi())
#    token = oauth.fetch_token('https://api.neur.io/v1/oauth2/token', code=code, client_id=self._get_clientid(), client_secret=self._get_clientsecret())
#    return token

# sensor local access; see https://api-docs.neur.io/#sensor-local-access
#
# GET http://[local_ipaddr]/current-sample


@REQUEST_TIME.time()
def collect(config, target):
    metric_to_gauge = {}
    def makegauge(metric, desc, labels=[]):
        already = metric_to_gauge.get(metric)
        if already:
            return already
        alllabels = ['sensor', 'phase', 'function'] + labels
        gmf = GaugeMetricFamily(metric, desc, labels=alllabels)
        metric_to_gauge[metric] = gmf
        return gmf

    targ = target if target.startswith('http') else 'http://%s' % target
    resp = requests.get('%s/current-sample' % targ)
    resp.raise_for_status()
    js = resp.json()
    sensorid = js.get('sensorId', '')
    for channel in js.get('channels', []):
        t = channel.get('type', '').lower()
        if t.startswith('phase_a_'):
            phase = 'A'
            f = t[8:]
        elif t.startswith('phase_b_'):
            phase = 'B'
            f = t[8:]
        elif t.startswith('phase_c_'):
            phase = 'C'
            f = t[8:]
        else:
            phase = 'total'
            f = t
        labels = [sensorid, phase, f]
        
        g = makegauge('imported_energy_ws', 'imported energy (Watt-seconds)')
        g.add_metric(labels, int(channel['eImp_Ws']))

        g = makegauge('exported_energy_ws', 'exported energy (Watt-seconds)')
        g.add_metric(labels, int(channel['eExp_Ws']))

        g = makegauge('power_w', 'instantaneous real power (Watts)')
        g.add_metric(labels, int(channel['p_W']))
 
        g = makegauge('instantaneous_var', 'instantaneous reactive power (Volt-Amps reactive)')
        g.add_metric(labels, int(channel['p_W']))

        g = makegauge('instantaneous_v', 'instantaneous voltage')
        g.add_metric(labels, float(channel['v_V']))

    return metric_to_gauge.values()
