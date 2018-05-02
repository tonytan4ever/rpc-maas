#!/usr/bin/env python

# Copyright 2017, Rackspace US, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import logging
import numpy as np
import os
import time
import yaml

import maas_common
from maas_common import status_err, status_ok, metric

import rally
from rally.api import API

from influxdb import InfluxDBClient

PLUGIN_CONF = '/etc/rally/maas_rally.yaml'
PLUGIN_PATH = '/usr/lib/rackspace-monitoring-agent/plugins/rally/plugins/'
TASKS_PATH = '/usr/lib/rackspace-monitoring-agent/plugins/rally/tasks/'
LOCKS_PATH = '/var/lock/maas_rally'


class ParseError(maas_common.MaaSException):
    pass


class CommandNotRecognized(maas_common.MaaSException):
    pass


class PluginConfig(object):
    def __init__(self, config_file):
        self.config_file = config_file
        self.config = {}

        self._load_config(config_file)

    def __getitem__(self, name):
        return self.config[name]

    def __getattr__(self, name):
        return self.config[name]

    def __str__(self):
        return str(self.config)

    def _load_config(self, config_file):
        with open(config_file, 'r') as stream:
            try:
                self.config = yaml.safe_load(stream)
            except Exception as e:
                raise Exception("Error while reading configuration file: {}"
                                .format(e))


def send_metrics_to_influxdb(plugin_config, logger):
    influx_config = plugin_config['influxdb']
    influx_host = influx_config['host']
    influx_port = influx_config['port']
    influx_database = influx_config['database']
    influx_user = influx_config['user']
    influx_password = influx_config['password']
    tags = influx_config['tags']

    client = InfluxDBClient(influx_host,
                            influx_port,
                            influx_user,
                            influx_password,
                            influx_database)

    influx_data = {}
    influx_data['measurement'] = args.task
    influx_data['tags'] = tags
    influx_data['fields'] = dict((k, float(v)) for k, v in
                                 maas_common.TELEGRAF_METRICS['variables']
                                 .iteritems())

    logger.debug("{} - writing metrics to influxdb database '{}' at {}".format(args.task, influx_database, influx_host + ':' + str(influx_port)))
    client.write_points([influx_data])
    logger.debug("{} - sent influx_data: {}".format(args.task, influx_data))


def setup_logging(plugin_config):
    logging.config.dictConfig(plugin_config['logging'])
    logger = logging.getLogger("maas_rally")
    return logger

def make_parser():
    parser = argparse.ArgumentParser(
        description='Execute rally performance scenario and print the results'
    )
    parser.add_argument('task',
                        help='Which task definition to execute.  The task '
                             'definition must exist in {{ maas_plugin_dir }}/ '
                             'tasks/<TASKNAME>.yml. \n'
                             'Examples: "keystone", "nova", etc.')
    parser.add_argument('-c', '--concurrency',
                        type=int,
                        help='Number of tasks to run in parallel')
    parser.add_argument('-e', '--extra_vars',
                        action='append',
                        help='Extra variable to pass to the Rally task in key='
                             'value format.  May be specified multiple times. '
                             'Example: "-e size=2 -e image_name=\'^cirros$\'"')
    parser.add_argument('-t', '--times',
                        type=int,
                        help='Number of times to execute the task')
    parser.add_argument('--telegraf-output',
                        action='store_true',
                        default=False,
                        help='Set the output format to telegraf')
    parser.add_argument('--influxdb',
                        action='store_true',
                        default=False,
                        help='Send output to influxdb')
    return parser


def parse_task_results(task_uuid, task_result, logger):
    # This expects the format returned by `rally task results <UUID>`
    action_data = {}
    action_data[args.task + '_total'] = list()
    for iteration in task_result['result']:
        iteration_total_duration = 0
        for action in iteration['atomic_actions'].keys():
            action_duration = iteration['atomic_actions'][action]
            iteration_total_duration += action_duration
            if action not in action_data:
                action_data[action] = list()
            action_data[action].append(action_duration)
        action_data[args.task + '_total'].append(iteration_total_duration)

    # Quota exceeded would be a typical error here
    if task_result['result'][0]['error']:
        logger.critical("{} - rally task {} encountered an error: {}".format(args.task, task_uuid, task_result['result'][0]['error']))
        status_err(' '.join(task_result['result'][0]['error']),
                   m_name='maas_rally')

    status_ok(m_name='maas_rally')

    metric('rally_load_duration', 'double',
           '{:.2f}'.format(task_result['load_duration']))
    metric('rally_full_duration', 'double',
           '{:.2f}'.format(task_result['full_duration']))

    metric('rally_sample_count', 'uint32',
           '{}'.format(task_result['key']['kw']['runner']['times']))
    metric('rally_sample_concurrency', 'uint32',
           '{}'.format(task_result['key']['kw']['runner']['concurrency']))

    for action in action_data.keys():
        metric('{}_min'.format(action), 'double',
               '{:.2f}'.format(np.amin(action_data[action])))
        metric('{}_max'.format(action), 'double',
               '{:.2f}'.format(np.amax(action_data[action])))
        metric('{}_median'.format(action), 'double',
               '{:.2f}'.format(np.median(action_data[action])))
        metric('{}_mean'.format(action), 'double',
               '{:.2f}'.format(np.mean(action_data[action])))

        metric('{}_90pctl'.format(action), 'double',
               '{:.2f}'.format(np.percentile(action_data[action], 90)))
        metric('{}_95pctl'.format(action), 'double',
               '{:.2f}'.format(np.percentile(action_data[action], 95)))


def main():
    start = time.time()
    plugin_config = PluginConfig(PLUGIN_CONF)
    logger = setup_logging(plugin_config)

    if logger.getEffectiveLevel() == logging.DEBUG:
        logger.debug("{} - maas_rally plugin started with args {}".format(args.task, args))
    else:
        logger.info("{} - maas_rally plugin started".format(args.task))

    # Ensure we can find the task definition
    tasks_path = os.path.realpath(TASKS_PATH)
    task_file = tasks_path + '/' + args.task + '.yml'
    if not os.path.isfile(task_file):
        logger.critical("{} - unable to locate task definition file {}".format(args.task, task_file))
        status_err('Unable to locate task definition '
                   'for {} in {}'.format(args.task, tasks_path),
                   m_name='maas_rally')
    else:
        logger.debug("{} - using task definition file {}".format(args.task, task_file))

    if not os.path.exists(LOCKS_PATH):
        os.makedirs(LOCKS_PATH)

    rapi = API()

    task_obj = rapi.task.create(args.task, [args.task])
    task_uuid = task_obj['uuid']
    logger.info("{} - allocated rally task ID {}".format(args.task, task_uuid))

    logger.debug("{} - checking for locks".format(args.task))
    LOCK_PATH = LOCKS_PATH + '/' + args.task + '/'
    if os.path.exists(LOCK_PATH):
        lock_uuid = os.listdir(LOCK_PATH)[0]
        lock_mtime = os.stat(LOCK_PATH + lock_uuid)[8]
        lock_duration = time.time() - lock_mtime
        logger.warning("{} - found existing lock for rally task {} from {} seconds ago".format(args.task, lock_uuid, lock_duration))
        try:
            logger.debug("{} - checking status for locking task {})".format(args.task, lock_uuid))
            task_status = rapi.task.get(lock_uuid)['status']
            logger.debug("{} - status for locking task {}: {})".format(args.task, lock_uuid, task_status))
            if task_status == 'finished':
                os.rmdir(LOCK_PATH + '/' + lock_uuid)
                logger.warning("{} - task {} was finished - removed lock".format(args.task, lock_uuid))
            elif task_status == 'init' and lock_duration > 30:
                logger.warning("{} - task {} was in init state for > 30 seconds - removed lock".format(args.task, lock_uuid))
                os.rmdir(LOCK_PATH + '/' + lock_uuid)
            else:
                logger.critical("{} - unable to remove lock by task ID {}".format(args.task, lock_uuid))
                lock_mtime_str = time.strftime('%H:%M:%S %Y-%m-%d %Z',
                                               time.localtime(lock_mtime))
                status_err("Unable to get lock for {} - locked by "
                           "task {} at {}.".format(args.task,
                                                   lock_uuid,
                                                   lock_mtime_str),
                           m_name='maas_rally')
        except rally.exceptions.TaskNotFound:
            os.rmdir(LOCK_PATH + '/' + lock_uuid)
            logger.warning("{} - task {} not found in rally db - removed lock".format(args.task, lock_uuid))
    else:
        logger.debug("{} - no lock found".format(args.task))
        os.mkdir(LOCK_PATH)

    os.mkdir(LOCK_PATH + '/' + task_uuid)
    logger.debug("{} - acquired lock for task {}".format(args.task, task_uuid))

    task_args = {}
    if args.times is not None:
        task_args['times'] = args.times
    if args.concurrency is not None:
        task_args['concurrency'] = args.concurrency
    if args.extra_vars is not None:
        for extra_var in args.extra_vars:
            k, v = extra_var.lstrip().split('=')
            task_args.update({k: v})

    with open(task_file) as f:
        input_task = f.read()
        task_dir = os.path.expanduser(
            os.path.dirname(task_file)) or "./"
        rendered_task = rapi.task.render_template(input_task,
                                                  task_dir,
                                                  **task_args)

    parsed_task = yaml.safe_load(rendered_task)

    logger.debug("{} - discovering rally plugins in {}".format(args.task, PLUGIN_PATH))
    rally.common.plugin.discover.load_plugins(PLUGIN_PATH)
    logger.debug("{} - loading rally plugins from {}".format(args.task, PLUGIN_PATH))
    rally.plugins.load()
    logger.info("{} - starting rally task {}".format(args.task, task_uuid))
    rapi.task.start(args.task, parsed_task, task_obj)

    # This is the format returned by `rally task results <UUID>`
    results = [{"key": x["key"], "result": x["data"]["raw"],
                "sla": x["data"]["sla"],
                "hooks": x["data"].get("hooks", []),
                "load_duration": x["data"]["load_duration"],
                "full_duration": x["data"]["full_duration"],
                "created_at": x.get("created_at").strftime(
                    "%Y-%d-%mT%H:%M:%S")}
               for x in task_obj.get_results()][0]

    if logger.getEffectiveLevel() == logging.DEBUG:
        logger.debug("{} - rally task {} completed with results: {}".format(args.task, task_uuid, results))
    else:
        logger.info("{} - rally task {} completed".format(args.task, task_uuid))

    parse_task_results(task_uuid, results, logger)

    os.rmdir(LOCK_PATH + '/' + task_uuid)
    os.rmdir(LOCK_PATH)
    logger.debug("{} - removed lock for rally task {}".format(args.task, task_uuid))

    end = time.time()
    metric('maas_check_duration', 'double', "{:.2f}".format((end - start) * 1))

    if args.influxdb:
        send_metrics_to_influxdb(plugin_config, logger)

    return


if __name__ == '__main__':
    parser = make_parser()
    args = parser.parse_args()
    with maas_common.print_output(print_telegraf=args.telegraf_output):
        main()
