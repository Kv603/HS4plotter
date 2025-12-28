# HS4plotter
Retrieve device values from Homeseer4 and upload to iotplotter, influxdb, grafana

This Python3 script depends on "requests" library for making HTTP/HTTPS calls, and (optionally) on "influxdb-client" to send data to InfluxDB.

If you known, for example, that you have a thermostat at reference ID 7 (you can find the reference number via the web UI), you can update the .ini and .netrc files for your HomeSeer and try discovering all the child devices by running the following:

    python hs4plotter.py --discover 7 --dry-run --verbose

I recommend running the tool from cron as a regular user, maybe every 5 minutes for a thermostat, as often as every minute for motion sensors.   If you're going to run this on the same Windows or Linux host as your HomeSeer4 instance, use venv to avoid intefering with any system python libraries.

You can create a free account at [https://iotplotter.com/](https://iotplotter.com/register.html).   Use "Create New Feed" to get your "FEED ID" and API key.    Populate these in the .ini file and then the graphs will be automatically generated the first time you run hs4plotter. 
