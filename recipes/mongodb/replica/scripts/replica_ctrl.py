"""
This script is used to setup and mantain a MongoDB replicase on a Docker Swarm.

It is intended to be used with the docker-compose.yml in the mongodb replica recipe.

IMPORTANT: There should not be more than one process running this script in the swarm.

HOW IT WORKS (Overview):
- Scans mongod instances in the swarm
- Picks an arbitrary instance to act as a replicaset primary
- Configures the replicaset on it
- Keeps on listening to changes in the original set of mongod instances
- Reconfigures replicaset if a change was perceived.

TODOS:
The script is full of room for improvements as you can see from the TODOs spread
in the code.

# TODO: Idempotency. If the node where this is running goes down, it will be
rescheduled to a different node. Instead of setting up a new replicaset it should
recognize the one already running.

# TODO: Add tests
"""
import docker
import logging
import pymongo as pm
import time

# TODO: Link these to env variables in docker-compose.yml
STACK_NAME = 'mongo-replica'
OVERLAY_NETWORK_NAME = '{}_replica'.format(STACK_NAME)
SERVICE_NAME_MONGO = '{}_mongo'.format(STACK_NAME)
REPLICASET_NAME = 'rs'
MONGO_PORT = 27017


def get_tasks_ips(tasks):
    tasks_ips = []
    for t in tasks:
        for n in t['NetworksAttachments']:
            if n['Network']['Spec']['Name'] == OVERLAY_NETWORK_NAME:
                # clean ip
                ip = n['Addresses'][0].split('/')[0]
                tasks_ips.append(ip)
    return tasks_ips


def create_mongo_config(tasks_ips):
    members = []
    for i, ip in enumerate(tasks_ips):
        members.append({
          '_id': i,
          'host': "{}:{}".format(ip, MONGO_PORT)
        })
    config = {'_id': REPLICASET_NAME, 'members': members}
    return config


def configure_replica(dc):
    logger = logging.getLogger(__name__)

    # TODO: add healthcheck in docker-compose and remove this sleep
    logger.info('Waiting some time before starting')
    time.sleep(42)

    # Get mongo tasks
    services = {s.name:s for s in dc.services.list()}
    if SERVICE_NAME_MONGO not in services:
        msg = "Error: Could not find mongo service. Did you correctly deploy the stack with both services?."
        logger.error(msg, exc_info=True)
        return

    mongo_service = services[SERVICE_NAME_MONGO]
    mongo_tasks = mongo_service.tasks()
    if len(mongo_tasks) == 0:
        msg = "Error: No task found for Mongo service. Missing a 'wait' time or healthcheck maybe?."
        logger.error(msg, exc_info=True)
        return

    # Get overlay network IPS of running mongo daemons.
    mongo_tasks_ips = get_tasks_ips(mongo_tasks)

    # Prepare mongo config
    config = create_mongo_config(mongo_tasks_ips)
    config['version'] = 1
    logger.info("Initial config: {}".format(config))

    # Choose a primary and configure replicaset
    primary_ip = str(mongo_tasks_ips[0])
    primary = pm.MongoClient(primary_ip, MONGO_PORT)
    res = primary.admin.command("replSetInitiate", config)
    logger.info("replSetInitiate: {}".format(res))

    # Respond to changes
    current_ips = set(mongo_tasks_ips)

    while True:
        # TODO: Reacting to events is better than sleeping and polling
        time.sleep(42)

        new_ips = set(get_tasks_ips(mongo_service.tasks()))
        if not new_ips.difference(current_ips):
            pass
        else:
            to_remove = set(current_ips) - set(new_ips)
            to_add = set(new_ips) - set(current_ips)
            assert to_remove or to_add

            # Reconfigure mongo replicaset
            # Actually not too different from what mongo does:
            # https://github.com/mongodb/mongo/blob/master/src/mongo/shell/utils.js
            if to_remove:
                # Note: As of writing, when a node goes down with a task running
                # a global service, Swarm is not tearing down that task and hence
                # this removal part has not been fully tested.
                logger.info("To remove: {}".format(to_remove))
                new_members = [m for m in config['members'] if m['host'].split(":")[0] in to_remove]
                config['members'] = new_members

            if to_add:
                logger.info("To add: {}".format(to_add))
                offset = max([m['_id'] for m in config['members']]) + 1
                for i, ip in enumerate(to_add):
                    config['members'].append({
                      '_id': offset + i,
                      'host': "{}:{}".format(ip, MONGO_PORT)
                    })

            if primary_ip in to_remove:
                # Find out who is the new primary and connect to it.
                # Solving this will solve the "Idempotency" of the script.
                # primary_ip = ?
                # primary = pm.MongoClient(primary_ip, MONGO_PORT)
                raise NotImplementedError("TODO: Primary failure recovery")

            config['version'] += 1
            logger.info("New config: {}".format(config))
            res = primary.admin.command("replSetReconfig", config)
            logger.info("replSetReconfig: {}".format(res))

            current_ips = new_ips


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    dc = docker.from_env()
    configure_replica(dc)
