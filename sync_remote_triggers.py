from tapiriik.database import db, close_connections
from tapiriik.settings import RABBITMQ_BROKER_URL, MONGO_FULL_WRITE_CONCERN
from datetime import datetime
from celery import Celery
from celery.signals import worker_shutdown

import json
import logging
logger = logging.getLogger(__name__)

class _celeryConfig:
    CELERY_ROUTES = {
        "sync_remote_triggers.trigger_remote": {"queue": "tapiriik-remote-trigger"}
    }
    CELERYD_CONCURRENCY = 1 # Otherwise the GC rate limiting breaks since file locking is per-process.
    CELERYD_PREFETCH_MULTIPLIER = 1 # The message queue could use some exercise.

celery_app = Celery('sync_remote_triggers', broker=RABBITMQ_BROKER_URL)
celery_app.config_from_object(_celeryConfig())

@worker_shutdown.connect
def celery_shutdown(**kwargs):
    close_connections()

@celery_app.task(acks_late=True)
def trigger_remote(service_id, affected_connection_external_ids_with_payloads):
    from tapiriik.auth import User
    affected_connection_ids = list()

    logger.debug("trigger_remote: external_ids = " + str(json.dumps(affected_connection_external_ids_with_payloads)))
    for item in affected_connection_external_ids_with_payloads:
        if isinstance(item, list):
            external_id, payload = item
        else:
            external_id = item
            payload = None
        logger.debug("trigger_remote: external_id = " + str(external_id))
        update_connection_query = {"$set":{"TriggerPartialSync": True, "TriggerPartialSyncTimestamp": datetime.utcnow()}}
        if payload is not None:
            update_connection_query.update({"$push": {"TriggerPartialSyncPayloads": payload, "$slice": -90}})
        logger.debug("trigger_remote: find_connection_query = " + str(json.dumps({"Service": service_id, "ExternalID": external_id})))
        logger.debug("trigger_remote: update_connection_query = " + str(json.dumps(update_connection_query)))
        record = db.connections.find_and_modify({"Service": service_id, "ExternalID": external_id}, update_connection_query, w=MONGO_FULL_WRITE_CONCERN)
        if record is not None:
            affected_connection_ids.append(record["_id"])
            logger.debug("trigger_remote: record['_id'] = " + str(record["_id"]))

    trigger_users_query = User.PaidUserMongoQuery()
    trigger_users_query.update({"ConnectedServices.ID": {"$in": affected_connection_ids}})
    trigger_users_query.update({"Config.suppress_auto_sync": {"$ne": True}})
    logger.debug("trigger_remote: trigger_users_query = " + str(json.dumps(trigger_users_query)))
    db.users.update(trigger_users_query, {"$set": {"NextSynchronization": datetime.utcnow()}}, multi=True) # It would be nicer to use the Sync.Schedule... method, but I want to cleanly do this in bulk
