# HS4plotter
Retrieve device values from Homeseer4 and upload to iotplotter, influxdb, grafana

This Python3 script depends on "requests" library for making HTTP/HTTPS calls, and (optionally) on influxdb-client to send data to InfluxDB.

If you known, for example, that you have a thermostat at reference ID 7 (you can find the reference number via the web UI), you can update the .ini and .netrc files for your HomeSeer and try discovering all the child devices by running the following:

    python hs4plotter.py --discover 7 --dry-run --verbose
