
import yaml

from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily, REGISTRY

# from https://github.com/prometheus/client_python#custom-collectors

class CustomCollector(object):
    def collect(self):
        yield GaugeMetricFamily('my_gauge', 'Help text', value=7)
        c = CounterMetricFamily('my_counter_total', 'Help text', labels=['foo'])
        c.add_metric(['bar'], 1.7)
        c.add_metric(['baz'], 3.8)
        yield c

REGISTRY.register(CustomCollector())

# SummaryMetricFamily, HistogramMetricFamily and InfoMetricFamily work similarly.

# A collector may implement a describe method which returns metrics in
# the same format as collect (though you don't have to include the
# samples). This is used to predetermine the names of time series a
# CollectorRegistry exposes and thus to detect collisions and
# duplicate registrations.

# Usually custom collectors do not have to implement describe. If
# describe is not implemented and the CollectorRegistry was created
# with auto_describe=True (which is the case for the default registry) then
# collect will be called at registration time instead of describe. If
# this could cause problems, either implement a proper describe, or if
# that's not practical have describe return an empty list.

config = yaml.load(open(configfile, 'rt'))

# config holds API keys for AmbientWeather

# our request will be
#      localhost:9115/probe?target=http://prometheus.io&module=neurio

# we should also serve /metrics from prometheus_client directly
#      - requests and latency for PurpleAir, AmbientWeather, Neurio
#
# and / that contains a form for /probe along with recent statuses,
#
# and /config to dump the configuration
