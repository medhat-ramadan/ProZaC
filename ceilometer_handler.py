"""
Class for polling Ceilometer
This class provides means to requests for authentication tokens to be used with OpenStack's Ceilometer, Nova and RabbitMQ
"""

__copyright__ = "Istituto Nazionale di Fisica Nucleare (INFN)"
__license__ = "Apache 2"

import struct
import urllib2
import json
import socket
import time
import logging
from threading import Timer
from exception_handler import *

class CeilometerHandler:
    def __init__(self, ceilometer_api_port, polling_interval, template_name, ceilometer_api_host, zabbix_host,
                 zabbix_port, zabbix_proxy_name, keystone_auth, keystone_host, compute_port, keystone_admin_port):
        """
        TODO
        :type self: object
        """
        self.ceilometer_api_port = ceilometer_api_port
        self.polling_interval = int(polling_interval)
        self.template_name = template_name
        self.ceilometer_api_host = ceilometer_api_host
        self.zabbix_host = zabbix_host
        self.zabbix_port = zabbix_port
        self.zabbix_proxy_name = zabbix_proxy_name
        self.keystone_auth = keystone_auth
        self.keystone_host = keystone_host
        self.compute_port = compute_port
        self.keystone_admin_port = keystone_admin_port
        full_token = keystone_auth.getToken()
        self.token = full_token['id']
        self.token_expires = full_token['expires']
        self.admin_tenantid = self.get_admin_tenantid()

        self.logger = logging.getLogger('ZCP')
        self.logger.info("Ceilometer handler initialized")

    def check_token_lifetime(self, expires_timestamp, threshold = 300):
        """
        check time (in seconds) left before token expiration
        if time left is below threshold, provides token renewal
        """
        now_timestamp_utc = time.time() + time.timezone
        timeleft = expires_timestamp - now_timestamp_utc

        if timeleft < threshold: # default, less than five minutes
            full_token = self.keystone_auth.getToken()
            self.token = full_token['id']
            self.token_expires = full_token['expires']
            self.logger.info("ceilometer token has been renewed")

    def connect_zabbix(self, payload):
        """
        Method used to send information to Zabbix
        :param payload: refers to the json message prepared to send to Zabbix
        :rtype : returns the response received by the Zabbix API
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((self.zabbix_host, int(self.zabbix_port)))
        s.send(payload)
        # read its response, the first five bytes are the header again
        response_header = s.recv(5, socket.MSG_WAITALL)
        if not response_header == 'ZBXD\1':
            raise ValueError('Got invalid response from Zabbix.')

        # read the data header to get the length of the response
        response_data_header = s.recv(8, socket.MSG_WAITALL)
        response_data_header = response_data_header[:4]
        response_len = struct.unpack('i', response_data_header)[0]

        # read the whole rest of the response now that we know the length
        response_raw = s.recv(response_len, socket.MSG_WAITALL)
        s.close()

        response = json.loads(response_raw)

        return response

    def get_admin_tenantid(self):
        tenantid = ""
        try:
            request = urllib2.urlopen(urllib2.Request("http://" + self.keystone_host + ":" + self.keystone_admin_port + "/v2.0/tenants",
                                      headers = {"Accept": "application/json", "Content-Type": "application/json","X-Auth-Token": self.token})).read()
        except urllib2.HTTPError, e:
            solved = handle_HTTPError_openstack(e, self)

        payload = json.loads(request)
        for item in payload['tenants']:
            if item['name'] == "admin":
                tenantid = item['id']

        return tenantid

    def get_hosts_ID(self):
        """
        Method used do query Zabbix API in order to fill an Array of hosts
        :return: returns a array of servers and items to monitor by server
        """
        try:
            request = urllib2.urlopen(urllib2.Request("http://" + self.keystone_host + ":" + self.compute_port + "/v2/" + self.admin_tenantid + "/servers/detail?all_tenants=1&status=ACTIVE",
                                      headers = {"Accept": "application/json", "Content-Type": "application/json", "X-Auth-Token": self.token})).read()
        except urllib2.HTTPError, e:
            solved = handle_HTTPError_openstack(e, self)
            if solved:
                self.update_values(self.get_hosts_ID())
                return

        payload = json.loads(request)
        hosts_id = []
        for item in payload['servers']:
            hosts_id.append([item['id'], item['name']])

        return hosts_id

    def query_ceilometer(self, resource_id, item_key, link):
        """
        TODO
        :param resource_id:
        :param item_key:
        :param link:
        """
        self.check_token_lifetime(self.token_expires)

        try:
            global contents
            contents = urllib2.urlopen(urllib2.Request(link + str("&limit=1"),
                                                       headers = {"Accept": "application/json", "Content-Type": "application/json", "X-Auth-Token": self.token})).read()
        except urllib2.HTTPError, e:
            solved = handle_HTTPError_openstack(e, self)
            if solved:
                self.query_ceilometer(resource_id, item_key, link)
                return

        response = json.loads(contents)
        try:
            counter_volume = response[0]['counter_volume']
            self.send_data_zabbix(counter_volume, resource_id, item_key)
        except:
            pass

    def run(self):
        Timer(self.polling_interval, self.run, ()).start()
        host_list = self.get_hosts_ID()
        self.update_values(host_list)

    def send_data_zabbix(self, counter_volume, resource_id, item_key):
        """
        Method used to prepare the body with data from Ceilometer and send it to Zabbix using connect_zabbix method
        :param counter_volume: the actual measurement
        :param resource_id:  refers to the resource ID
        :param item_key:    refers to the item key
        """
        tmp = json.dumps(counter_volume)
        data = {"request": "history data", "host": self.zabbix_proxy_name,
                "data": [{"host": resource_id, "key": item_key, "value": tmp}]}

        payload = self.set_proxy_header(data)
        self.connect_zabbix(payload)

    def set_proxy_header(self, data):
        """
        Method used to simplify constructing the protocol to communicate with Zabbix
        :param data: refers to the json message
        :rtype : returns the message ready to send to Zabbix server with the right header
        """
        data_length = len(data)
        data_header = struct.pack('i', data_length) + '\0\0\0\0'
        HEADER = '''ZBXD\1%s%s'''
        data_to_send = HEADER % (data_header, data)
        payload = json.dumps(data)
        return payload

    def update_values(self, hosts_id):
        """
        Queries Ceilometer for new samples.
        :param hosts_id:
        """
        self.check_token_lifetime(self.token_expires)

        if not hosts_id:
            self.logger.info('No active instances. Nothing to monitor.')
            return

        for host in hosts_id:
            links = []
            if not host[1] == self.template_name:
                self.logger.info("Checking host %s" %(host[1]))
                #Get links for instance compute metrics
                try:
                    request = urllib2.urlopen(urllib2.Request("http://" + self.ceilometer_api_host + ":" + self.ceilometer_api_port + "/v2/resources?q.field=resource_id&q.value=" + host[0],
                                              headers = {"Accept": "application/json", "Content-Type": "application/json", "X-Auth-Token": self.token})).read()
                except urllib2.HTTPError, e:
                    solved = handle_HTTPError_openstack(e, self)
                    if solved:
                        self.update_values(hosts_id)
                        return

                # Filter the links to an array
                for line in json.loads(request):
                    for line2 in line['links']:
                        if line2['rel'] in ('cpu_util', 'memory', 'disk.root.size', 'vcpus'):
                            links.append(line2)

                # Get the links regarding network metrics
                try:
                    request = urllib2.urlopen(urllib2.Request("http://" + self.ceilometer_api_host + ":" + self.ceilometer_api_port + "/v2/resources?q.field=metadata.instance_id&q.value=" + host[0],
                                                              headers = {"Accept": "application/json","Content-Type": "application/json", "X-Auth-Token": self.token})).read()
                except urllib2.HTTPError, e:
                    solved = handle_HTTPError_openstack(e, self)
                    if solved:
                        self.update_values(hosts_id)
                        return

                # Add more links to the array
                for line in json.loads(request):
                    for line2 in line['links']:
                        if line2['rel'] in ('network.incoming.bytes.rate', 'network.outgoing.bytes.rate'):
                            links.append(line2)

                # Query ceilometer API using the array of links
                for line in links:
                    self.query_ceilometer(host[0], line['rel'], line['href'])
                    self.logger.debug ('- Item ' + (line['rel']))
