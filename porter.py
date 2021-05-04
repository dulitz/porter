# porter.py
#
# uses the multi-target exporter pattern
# (https://prometheus.io/docs/guides/multi-target-exporter/)
# to connect various external services to Prometheus.
#
# the main advantage of this pattern is that the data is current at the time Prometheus
# issues the query. also you can control the frequency of queries, and what is queried,
# by editing only the Prometheus configuration. this exporter can just run forever in a
# container.
#
# currently supports PurpleAir and Ambient Weather.

import json, prometheus_client, requests, time, yaml

from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import REGISTRY

import ambientweather, purpleair
from prometheus import start_wsgi_server

# /probe&target=80845&module=purpleair

# see https://github.com/prometheus/client_python#custom-collectors

# TODO: response code histograms
# TODO: histograms of exceptions at top level
# TODO: serve / with form for /probe along with recent statuses
# TODO: serve /config to dump the configuration
# TODO: use more of the config file


class ProbeCollector(object):
    def __init__(self, config):
        self.config = config

    def collect(self):
        return iter([])
        
    def collect2(self, path, params):
        targets = params.get('target', [])
        rawmodule = params.get('module')
        module = rawmodule[0] if rawmodule and len(rawmodule) == 1 else ''
        if module and path.startswith('/probe'):
            try:
                if module == 'purpleair':
                    for target in targets:
                        for metric in purpleair.collect(self.config, target):
                            yield metric
                elif module == 'ambientweather':
                    for target in targets:
                        for metric in ambientweather.collect(self.config, target):
                            yield metric
                elif module == 'neurio' or module == 'pwrview':
                    pass
                else:
                    print('unknown module %s' % (params))
                    yield GaugeMetricFamily('ignore', 'ignore')
            except json.JSONDecodeError:
                raise
            except requests.exceptions.HTTPError:
                raise
        else:
            print('unknown request %s %s' % (path, params))
            yield GaugeMetricFamily('ignore', 'ignore')


class Porter:
    def __init__(self, config):
        self.config = config
        port = self.config.get('port')
        if not port:
            self.config['port'] = 8000
        REGISTRY.register(ProbeCollector(config))

    def start_wsgi_server(self, port=0):
        if not port:
            port = self.config['port']
        print('serving on port %d' % port)
        start_wsgi_server(port)


def main(args):
    configfile = 'porter.yml'
    if len(args) > 1:
        configfile = args[1]

    config = yaml.safe_load(open(configfile, 'rt')) or {}
    if config:
        print('using configuration file %s' % configfile)
    else:
        print('configuration file %s was empty; ignored' % configfile)
    p = Porter(config)
    p.start_wsgi_server()

    while True:
        time.sleep(1)


if __name__ == '__main__':
    import sys
    sys.exit(main(sys.argv))
