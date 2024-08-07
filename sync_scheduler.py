from tapiriik.database import db
from tapiriik.sync import Sync
from datetime import datetime
from pymongo.read_preferences import ReadPreference
import kombu
import time
import uuid
import logging

Sync.InitializeWorkerBindings()

producer = kombu.Producer(Sync._channel, Sync._exchange)

logger = logging.getLogger(__name__)

while True:
    generation = str(uuid.uuid4())
    queueing_at = datetime.utcnow()
    users = list(db.users.with_options(read_preference=ReadPreference.PRIMARY).find(
                {
                    "NextSynchronization": {"$lte": datetime.utcnow()},
                    "QueuedAt": {"$exists": False}
                },
                {
                    "_id": True,
                    "SynchronizationHostRestriction": True
                }
            ))
    scheduled_ids = [x["_id"] for x in users]
    scheduled_ids_len = len(scheduled_ids)
    if scheduled_ids_len > 0:
        logger.info("Found %d users at %s" % (len(scheduled_ids), datetime.utcnow()))
        db.users.update({"_id": {"$in": scheduled_ids}}, {"$set": {"QueuedAt": queueing_at, "QueuedGeneration": generation}, "$unset": {"NextSynchronization": True}}, multi=True)
        logger.info("Marked %d users as queued at %s" % (len(scheduled_ids), datetime.utcnow()))
        for user in users:
            # logger.info("Set QueuedAt for user " + str(user["_id"]) + " and publish it to queue")
            producer.publish({"user_id": str(user["_id"]), "generation": generation}, routing_key=user["SynchronizationHostRestriction"] if "SynchronizationHostRestriction" in user and user["SynchronizationHostRestriction"] else "")
        logger.info("Scheduled %d users at %s" % (len(scheduled_ids), datetime.utcnow()))

    time.sleep(1)
