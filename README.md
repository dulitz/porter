# porter

Porter is a Prometheus multi-target exporter focused on environmental data and "home status,"
for your "home dashboard."

* PurpleAir

[PurpleProm](https://github.com/steventblack/purpleprom)

This is in Go but otherwise has a clean interface.

e.g. In the URL https://www.purpleair.com/map?opt=1/mAQI/a10/cC0&select=37011#15.94/37.437227/-122.198933, the sensor ID is 37011.

[purpleair-to-prometheus](https://github.com/wbertelsen/purpleair-to-prometheus/blob/main/purple_to_prom.py)

This is one file.

* Ambient Weather

[third party API on Github](https://github.com/avryhof/ambient_api)

This installs a lot of stuff and has janky configuration, but it's not much code
so I'll fork it.

* Neurio / Generac PWRview

* Prometheus-client for Python

[Multi-Target Exporter Guide](https://prometheus.io/docs/guides/multi-target-exporter/)
