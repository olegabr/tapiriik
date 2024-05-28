from tapiriik.settings import WEB_ROOT, HTTP_SOURCE_ADDR, VTREKE_CLIENT_SECRET, VTREKE_CLIENT_ID, VTREKE_RATE_LIMITS
from tapiriik.services.service_base import ServiceAuthenticationType, ServiceBase
from tapiriik.services.service_record import ServiceRecord
from tapiriik.database import cachedb, db
from tapiriik.services.interchange import UploadedActivity, ActivityType, ActivityStatistic, ActivityStatisticUnit, Waypoint, WaypointType, Location, Lap
from tapiriik.services.api import APIException, UserException, UserExceptionType, APIExcludeActivity
from tapiriik.services.fit import FITIO
from tapiriik.auth import User

from django.core.urlresolvers import reverse
from datetime import datetime, timedelta
from urllib.parse import urlencode
import calendar
import requests
import os
import logging
import pytz
import re
import time
import json
import tempfile

logger = logging.getLogger(__name__)

class VTrekeService(ServiceBase):
    ID = "vtreke"
    DisplayName = "VTreke"
    DisplayAbbreviation = "VT"
    AuthenticationType = ServiceAuthenticationType.UsernamePassword
    UserProfileURL = "https://vtreke.ru/profile/{0}"
    UserActivityURL = "https://vtreke.ru/?status/{1}"
    API_URL = "https://vtreke.ru/wp-json/peepso/v1/activity_upload"
    AuthenticationNoFrame = True  # They don't prevent the iframe, it just looks really ugly.
    PartialSyncRequiresTrigger = True
    PartialSyncTriggerStatusCode = 200
    PartialSyncTriggerRequiresSubscription = True
    LastUpload = None

    SupportsHR = SupportsCadence = SupportsTemp = SupportsPower = True

    SupportsActivityDeletion = False

    # For mapping common->VTreke; no ambiguity in VTreke activity type
    _activityTypeMappings = {
        ActivityType.Cycling: "Ride",
        ActivityType.MountainBiking: "Ride",
        ActivityType.Hiking: "Hike",
        ActivityType.Running: "Run",
        ActivityType.Walking: "Walk",
        ActivityType.Snowboarding: "Snowboard",
        ActivityType.Skating: "IceSkate",
        ActivityType.CrossCountrySkiing: "NordicSki",
        ActivityType.DownhillSkiing: "AlpineSki",
        ActivityType.Swimming: "Swim",
        ActivityType.Gym: "Workout",
        ActivityType.Rowing: "Rowing",
        ActivityType.Elliptical: "Elliptical",
        ActivityType.RollerSkiing: "RollerSki",
        ActivityType.StrengthTraining: "WeightTraining",
        ActivityType.Climbing: "RockClimbing",
        ActivityType.StandUpPaddling: "StandUpPaddling",
    }

    # For mapping VTreke->common
    _reverseActivityTypeMappings = {
        "Ride": ActivityType.Cycling,
        "VirtualRide": ActivityType.Cycling,
        "EBikeRide": ActivityType.Cycling,
        "MountainBiking": ActivityType.MountainBiking,
        "VirtualRun": ActivityType.Running,
        "Run": ActivityType.Running,
        "Hike": ActivityType.Hiking,
        "Walk": ActivityType.Walking,
        "AlpineSki": ActivityType.DownhillSkiing,
        "CrossCountrySkiing": ActivityType.CrossCountrySkiing,
        "NordicSki": ActivityType.CrossCountrySkiing,
        "BackcountrySki": ActivityType.DownhillSkiing,
        "Snowboard": ActivityType.Snowboarding,
        "Swim": ActivityType.Swimming,
        "IceSkate": ActivityType.Skating,
        "Workout": ActivityType.Gym,
        "Rowing": ActivityType.Rowing,
        "Kayaking": ActivityType.Rowing,
        "Canoeing": ActivityType.Rowing,
        "StandUpPaddling": ActivityType.StandUpPaddling,
        "Elliptical": ActivityType.Elliptical,
        "RollerSki": ActivityType.RollerSkiing,
        "WeightTraining": ActivityType.StrengthTraining,
        "RockClimbing" : ActivityType.Climbing,
    }

    SupportedActivities = list(_activityTypeMappings.keys())

    GlobalRateLimits = VTREKE_RATE_LIMITS
    # GlobalRateLimitsPreemptiveSleep = True

    def UserUploadedActivityURL(self, uploadId):
        return "https://vtreke.ru/?status/%d" % uploadId

    def WebInit(self):
        self.UserAuthorizationURL = "https://vtreke.ru/"

    def Authorize(self, email, password):
        # email is wordpress user_id, and password is 1 if we need to register user, and 0 otherwise
        from tapiriik.auth.credential_storage import CredentialStore
        self._rate_limit()
        return (email, {}, {"Email": CredentialStore.Encrypt(email), "Password": CredentialStore.Encrypt(password)})

    def __init__(self):
        rate_lock_path = tempfile.gettempdir() + "/vtreke_rate.%s.lock" % HTTP_SOURCE_ADDR
        # Ensure the rate lock file exists (...the easy way)
        open(rate_lock_path, "a").close()
        self._rate_lock = open(rate_lock_path, "r+")
        

    def _rate_limit(self):
        import fcntl, struct, time
        min_period = 1  # I appear to been banned from Garmin Connect while determining this.
        fcntl.flock(self._rate_lock,fcntl.LOCK_EX)
        try:
            self._rate_lock.seek(0)
            last_req_start = self._rate_lock.read()
            if not last_req_start:
                last_req_start = 0
            else:
                last_req_start = float(last_req_start)

            wait_time = max(0, min_period - (time.time() - last_req_start))
            time.sleep(wait_time)

            self._rate_lock.seek(0)
            self._rate_lock.write(str(time.time()))
            self._rate_lock.flush()
        finally:
            fcntl.flock(self._rate_lock,fcntl.LOCK_UN)

    def _requestWithAuth(self, reqLambda, serviceRecord):
        self._globalRateLimit()

        session = requests.Session()
        session.headers.update({"X-VTREKE-API-SECRET": VTREKE_CLIENT_SECRET})

        return reqLambda(session)

    def RevokeAuthorization(self, serviceRecord):
        # nothing to do here...
        pass

    def DeleteCachedData(self, serviceRecord):
        # nothing cached...
        pass

    def DownloadActivityList(self, svcRecord, exhaustive=False):
        activities = []
        exclusions = []
        return activities, exclusions

    def SubscribeToPartialSyncTrigger(self, serviceRecord):
        # There is no per-user webhook subscription with VTreke.
        serviceRecord.SetPartialSyncTriggerSubscriptionState(True)

    def UnsubscribeFromPartialSyncTrigger(self, serviceRecord):
        # As above.
        serviceRecord.SetPartialSyncTriggerSubscriptionState(False)

    def ExternalIDsForPartialSyncTrigger(self, req):
        data = json.loads(req.body.decode("UTF-8"))
        return [(data["owner_id"], None)]

    def PartialSyncTriggerGET(self, req):
        # VTreke requires this endpoint to echo back a challenge.
        # Only happens once during manual endpoint setup?
        from django.http import HttpResponse
        return HttpResponse(json.dumps({
            "hub.challenge": req.GET["hub.challenge"]
        }))

    def DownloadActivity(self, svcRecord, activity):
        return activity

    def UploadActivity(self, serviceRecord, activity):
        logger.info("Activity tz " + str(activity.TZ) + " dt tz " + str(activity.StartTime.tzinfo) + " starttime " + str(activity.StartTime))

        if serviceRecord.HasExtendedAuthorizationDetails():
            extendedAuthorization = serviceRecord.ExtendedAuthorization
            from tapiriik.auth.credential_storage import CredentialStore
            register = CredentialStore.Decrypt(extendedAuthorization["Password"])
            logger.debug("Activity register = " + str(register))
            if (str(register) != 1):
                logger.info("UploadActivity not processed: register = " + str(register))
                # existingUser = User.AuthByService(serviceRecord)
                # # only log us in as this different user in the case that we don't already have an account
                # if existingUser is not None:
                #     User.Login(existingUser, req)
                return

        if self.LastUpload is not None:
            while (datetime.now() - self.LastUpload).total_seconds() < 5:
                time.sleep(1)
                logger.debug("Inter-upload cooldown")
        source_svc = None
        if hasattr(activity, "ServiceDataCollection"):
            source_svc = str(list(activity.ServiceDataCollection.keys())[0])

        upload_id = None
        if activity.CountTotalWaypoints():
            if serviceRecord.HasExtendedAuthorizationDetails():
                extendedAuthorization = serviceRecord.ExtendedAuthorization
                from tapiriik.auth.credential_storage import CredentialStore
                wp_user_id = CredentialStore.Decrypt(extendedAuthorization["Email"])
                logger.info("Activity wp_user_id = " + str(wp_user_id))

            req = {
                    "data_type": "fit",
                    "activity_name": activity.Name,
                    "description": activity.Notes, # Paul Mach said so.
                    "activity_type": self._activityTypeMappings[activity.Type],
                    "private": 1 if activity.Private else 0,
                    "user_id": wp_user_id}

            if "fit" in activity.PrerenderedFormats:
                logger.debug("Using prerendered FIT")
                fitData = activity.PrerenderedFormats["fit"]
            else:
                # TODO: put the fit back into PrerenderedFormats once there's more RAM to go around and there's a possibility of it actually being used.
                fitData = FITIO.Dump(activity, drop_pauses=True)
            files = {"file":("tap-sync-" + activity.UID + "-" + str(os.getpid()) + ("-" + source_svc if source_svc else "") + ".fit", fitData)}

            response = self._requestWithAuth(lambda session: session.post(self.API_URL, data=req, files=files), serviceRecord)
            if response.status_code != 201:
                if response.status_code == 401:
                    raise APIException("No authorization to upload activity " + activity.UID + " response " + response.text + " status " + str(response.status_code), block=True, user_exception=UserException(UserExceptionType.Authorization, intervention_required=True))
                if "duplicate of activity" in response.text:
                    logger.debug("Duplicate")
                    self.LastUpload = datetime.now()
                    return # Fine by me. The majority of these cases were caused by a dumb optimization that meant existing activities on services were never flagged as such if tapiriik didn't have to synchronize them elsewhere.
                raise APIException("Unable to upload activity " + activity.UID + " response " + response.text + " status " + str(response.status_code))

            upload_id = response.json()["id"]
        # else:
        #     localUploadTS = activity.StartTime.strftime("%Y-%m-%d %H:%M:%S")
        #     req = {
        #             "name": activity.Name if activity.Name else activity.StartTime.strftime("%d/%m/%Y"), # This is required
        #             "description": activity.Notes,
        #             "type": self._activityTypeMappings[activity.Type],
        #             "private": 1 if activity.Private else 0,
        #             "start_date_local": localUploadTS,
        #             "distance": activity.Stats.Distance.asUnits(ActivityStatisticUnit.Meters).Value,
        #             "elapsed_time": round((activity.EndTime - activity.StartTime).total_seconds())
        #         }
        #     response = self._requestWithAuth(lambda session: session.post("https://vtreke.ru/api/v3/activities", data=req), serviceRecord)
        #     # FFR this method returns the same dict as the activity listing, as REST services are wont to do.
        #     if response.status_code != 201:
        #         if response.status_code == 401:
        #             raise APIException("No authorization to upload activity " + activity.UID + " response " + response.text + " status " + str(response.status_code), block=True, user_exception=UserException(UserExceptionType.Authorization, intervention_required=True))
        #         raise APIException("Unable to upload stationary activity " + activity.UID + " response " + response.text + " status " + str(response.status_code))
        #     upload_id = response.json()["id"]

        self.LastUpload = datetime.now()
        return upload_id

    def DeleteActivity(self, serviceRecord, uploadId):
        # do nothing
        pass
