#!/usr/bin/env python3

import collections
import csv
import datetime
import json
import logging
import io
import os
import time
import urllib.request

from influxdb import InfluxDBClient

FUEL_MIX="http://mis.nyiso.com/public/csv/rtfuelmix/{0}rtfuelmix.csv"

_LOGGER = logging.getLogger(__name__)
logging.basicConfig(
    format='%(asctime)s %(levelname)s %(message)s',
    level=logging.DEBUG)

HOST = os.environ.get("INFLUXDB_HOST")

LAST = 0

# PWR in MWh
# CO2 in Metric Tons

FUEL_2016 = {
    "Petroleum": {
        "Power": 642952,
        "CO2": 623836
    },
    "Natural Gas": {
        "Power": 56793336,
        "CO2": 26865277
    }
}

# assume Dual Fuel systems consume 30% of state NG. That's probably low.
FUEL_2016["Dual Fuel"] = {
    "Power": (FUEL_2016["Petroleum"]["Power"] + (FUEL_2016["Natural Gas"]["Power"] * .3)),
    "CO2": (FUEL_2016["Petroleum"]["CO2"] + (FUEL_2016["Natural Gas"]["CO2"] * .3)),
}

# Calculate CO2 per kWh usage

def co2_for_fuel(fuel):
    if fuel in FUEL_2016:
        hpow = FUEL_2016[fuel]["Power"]
        hco2 = FUEL_2016[fuel]["CO2"]
        co2per = float(hco2) / float(hpow)
        return co2per
    else:
        return 0.0

def get_pass():
    with open("/etc/secret-volume/password") as f:
        return f.read()

def collect_data(when=None):
    if not when:
        when = datetime.datetime.now()
    url = FUEL_MIX.format(when.strftime("%Y%m%d"))

    # unfortunately we can't quite connect urllib to csv
    with urllib.request.urlopen(url) as response:
        out = io.StringIO()
        out.write(response.read().decode('utf-8'))

    # We have to rewind the output stream so it can be read by
    # csv.reader
    out.seek(0)
    reader = csv.reader(out, quoting=csv.QUOTE_NONE)
    data = collections.OrderedDict()

    # this folds up the data as a hash area keyed by timestamp for
    # easy sorting
    for row in reader:
        try:
            timestamp = timestamp2epoch(row[0])
            if timestamp in data:
                data[timestamp].append(row)
            else:
                data[timestamp] = [row]
        except ValueError:
            # skip a parse error on epoch, as it's table headers.
            pass

    return data


def on_connect(client, userdata, flags, rc):
    _LOGGER.info("Connected to mqtt bus")
    client.subscribe("ny-power/updated/fuel-mix")

# NOTE(sdague): there is a bootstrapping problem here
def on_message(client, userdata, msg):
    if msg.topic == "ny-power/updated/fuel-mix":
        global LAST
        data = json.loads(msg.payload.decode('utf-8'))
        LAST = timestamp2epoch(data["ts"])


def timestamp2epoch(ts):
    return int(datetime.datetime.strptime(ts, "%m/%d/%Y %H:%M:%S").strftime("%s"))

def mqtt_client():
    client = mqtt.Client(clean_session=True)
    client.username_pw_set("pump", get_pass())
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(HOST)
    client.loop_start()
    return client

def catchup_influxdb(client, data):
    for timestamp, rowset in data.items():

        total_kW = 0
        total_co2 = 0

        for row in rowset:
            strtime = row[0]
            fuel_name = row[2]
            kW = int(float(row[3]))
            _LOGGER.info("publish ny-power/fuel-mix/%s => %s" % (fuel_name, strtime))
            pkt = [
                {
                    "measurement": "fuel_mix_current",
                    "tags": {
                        "fuel_type": fuel_name
                    },
                    "time": strtime,
                    "fields": {

                        "value": kW
                    }
                }
            ]
            client.write_points(pkt)
            total_kW += kW
            total_co2 += kW * co2_for_fuel(fuel_name)


        # send out co2 batch
        co2_per_kW = total_co2 / total_kW
        pkt = [
            {
                "measurement": "co2_current",
                "time": strtime,
                "fields": {
                    "value": co2_per_kW
                }
            }
        ]
        client.write_points(pkt)
        # client.publish("ny-power/co2",
        #                json.dumps(dict(ts=strtime, emissions=co2_per_kW, units="kg / kWh")),
        #                qos=1, retain=True)

        # client.publish("ny-power/updated/fuel-mix",
        #                json.dumps(dict(ts=strtime)), qos=1, retain=True)


def main():
    client = InfluxDBClient(HOST, 8086, 'root', 'root', 'fuel-mix')
    dbs = [x['name'] for x in client.get_list_database()]
    if 'fuel-mix' not in dbs:
        client.create_database('fuel-mix')
    for r in range(0, 7):
        when = datetime.datetime.now() - datetime.timedelta(days=r)
        data = collect_data(when)
        catchup_influxdb(client, data)


if __name__ == "__main__":
    main()