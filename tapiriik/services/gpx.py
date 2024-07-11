from lxml import etree
from pytz import UTC
import copy
import dateutil.parser
from datetime import datetime
from .interchange import WaypointType, Activity, Waypoint, Location, Lap, ActivityStatistic, ActivityStatisticUnit
from .interchange import ActivityType
from .statistic_calculator import ActivityStatisticCalculator

class GPXIO:
    Namespaces = {
        None: "http://www.topografix.com/GPX/1/1",
        "gpxtpx": "http://www.garmin.com/xmlschemas/TrackPointExtension/v1",
        "gpxdata": "http://www.cluetrust.com/XML/GPXDATA/1/0",
        "gpxext": "http://www.garmin.com/xmlschemas/GpxExtensions/v3",
        "gpxtrkx": "http://www.garmin.com/xmlschemas/TrackStatsExtension/v1"
    }

    def Parse(gpxData, activity=None, suppress_validity_errors=False):
        ns = copy.deepcopy(GPXIO.Namespaces)
        ns["gpx"] = ns[None]
        del ns[None]
        act = Activity() if not activity else activity

        act.GPS = True # All valid GPX files have GPS data

        try:
            root = etree.XML(gpxData)
        except:
            root = etree.fromstring(gpxData)

        # GPSBabel produces files with the GPX/1/0 schema - I have no clue what's new in /1
        # So, blindly accept whatever we're given!
        ns["gpx"] = root.nsmap[None]

        xmeta = root.find("gpx:metadata", namespaces=ns)
        if xmeta is not None:
            xname = xmeta.find("gpx:name", namespaces=ns)
            if xname is not None:
                act.Name = xname.text
        xtrk = root.find("gpx:trk", namespaces=ns)

        if xtrk is None:
            raise ValueError("Invalid GPX")

        xtrksegs = xtrk.findall("gpx:trkseg", namespaces=ns)
        startTime = None
        endTime = None

        for xtrkseg in xtrksegs:
            lap = Lap()
            for xtrkpt in xtrkseg.findall("gpx:trkpt", namespaces=ns):
                wp = Waypoint()

                wp.Timestamp = dateutil.parser.parse(xtrkpt.find("gpx:time", namespaces=ns).text)
                wp.Timestamp.replace(tzinfo=UTC)
                if startTime is None or wp.Timestamp < startTime:
                    startTime = wp.Timestamp
                if endTime is None or wp.Timestamp > endTime:
                    endTime = wp.Timestamp

                wp.Location = Location(float(xtrkpt.attrib["lat"]), float(xtrkpt.attrib["lon"]), None)
                eleEl = xtrkpt.find("gpx:ele", namespaces=ns)
                if eleEl is not None:
                    wp.Location.Altitude = float(eleEl.text)
                extEl = xtrkpt.find("gpx:extensions", namespaces=ns)
                if extEl is not None:
                    gpxtpxExtEl = extEl.find("gpxtpx:TrackPointExtension", namespaces=ns)
                    if gpxtpxExtEl is not None:
                        hrEl = gpxtpxExtEl.find("gpxtpx:hr", namespaces=ns)
                        if hrEl is not None:
                            wp.HR = float(hrEl.text)
                        cadEl = gpxtpxExtEl.find("gpxtpx:cad", namespaces=ns)
                        if cadEl is not None:
                            wp.Cadence = float(cadEl.text)
                        tempEl = gpxtpxExtEl.find("gpxtpx:atemp", namespaces=ns)
                        if tempEl is not None:
                            wp.Temp = float(tempEl.text)
                    gpxdataHR = extEl.find("gpxdata:hr", namespaces=ns)
                    if gpxdataHR is not None:
                        wp.HR = float(gpxdataHR.text)
                    gpxdataCadence = extEl.find("gpxdata:cadence", namespaces=ns)
                    if gpxdataCadence is not None:
                        wp.Cadence = float(gpxdataCadence.text)
                lap.Waypoints.append(wp)
            act.Laps.append(lap)
            if not len(lap.Waypoints) and not suppress_validity_errors:
                raise ValueError("Track segment without points")
            elif len(lap.Waypoints):
                lap.StartTime = lap.Waypoints[0].Timestamp
                lap.EndTime = lap.Waypoints[-1].Timestamp

        if not len(act.Laps) and not suppress_validity_errors:
            raise ValueError("File with no track segments")

        if act.CountTotalWaypoints():
            act.GetFlatWaypoints()[0].Type = WaypointType.Start
            act.GetFlatWaypoints()[-1].Type = WaypointType.End
            act.Stats.Distance = ActivityStatistic(ActivityStatisticUnit.Meters, value=ActivityStatisticCalculator.CalculateDistance(act))

            if len(act.Laps) == 1:
                # GPX encodes no real per-lap/segment statistics, so this is the only case where we can fill this in.
                # I've made an exception for the activity's total distance, but only because I want it later on for stats.
                act.Laps[0].Stats = act.Stats

        act.Stationary = False
        act.StartTime = startTime
        act.EndTime = endTime

        act.CalculateUID()
        return act

    def Dump(activity):
        GPXTPX = "{" + GPXIO.Namespaces["gpxtpx"] + "}"
        GPXTSX = "{" + GPXIO.Namespaces["gpxtrkx"] + "}"
        root = etree.Element("gpx", nsmap=GPXIO.Namespaces)
        root.attrib["creator"] = "tapiriik-sync"
        meta = etree.SubElement(root, "metadata")
        trk = etree.SubElement(root, "trk")
        if activity.Stationary:
            raise ValueError("Please don't use GPX for stationary activities.")
        if activity.Name is not None:
            etree.SubElement(meta, "name").text = activity.Name
            etree.SubElement(trk, "name").text = activity.Name

        # FIT doesn't have different fields for this, but it does have a different interpretation - we eventually need to divide by two in the running case.
        # Further complicating the issue is that most sites don't differentiate the two, so they'll end up putting the run cadence back into the bike field.
        use_run_cadence = activity.Type in [ActivityType.Running, ActivityType.Walking, ActivityType.Hiking]
        def _resolveRunCadence(bikeCad, runCad):
            nonlocal use_run_cadence
            if use_run_cadence:
                return runCad if runCad is not None else (bikeCad if bikeCad is not None else None)
            else:
                return bikeCad

        def _mapStat(dict, key, value):
            if value is not None:
                dict[key] = value

        def _makeTrackStatsExtension(extensions, stats):
            gpxtsxexts = etree.SubElement(extensions, GPXTSX + "TrackStatsExtension")
            if "total_distance" in stats:
                etree.SubElement(gpxtsxexts, GPXTSX + "Distance").text = str(float(stats["total_distance"]))
            if "total_timer_time" in stats:
                etree.SubElement(gpxtsxexts, GPXTSX + "TimerTime").text = str(int(stats["total_timer_time"]))
            if "total_elapsed_time" in stats:
                etree.SubElement(gpxtsxexts, GPXTSX + "TotalElapsedTime").text = str(int(stats["total_elapsed_time"]))
            if "total_moving_time" in stats:
                etree.SubElement(gpxtsxexts, GPXTSX + "MovingTime").text = str(int(stats["total_moving_time"]))
            if "total_stopped_time" in stats:
                etree.SubElement(gpxtsxexts, GPXTSX + "StoppedTime").text = str(int(stats["total_stopped_time"]))
            if "avg_speed" in stats:
                etree.SubElement(gpxtsxexts, GPXTSX + "MovingSpeed").text = str(float(stats["avg_speed"]))
            if "max_speed" in stats:
                etree.SubElement(gpxtsxexts, GPXTSX + "MaxSpeed").text = str(float(stats["max_speed"]))
            if "max_altitude" in stats:
                etree.SubElement(gpxtsxexts, GPXTSX + "MaxElevation").text = str(float(stats["max_altitude"]))
            if "min_altitude" in stats:
                etree.SubElement(gpxtsxexts, GPXTSX + "MinElevation").text = str(float(stats["min_altitude"]))
            if "total_ascent" in stats:
                etree.SubElement(gpxtsxexts, GPXTSX + "Ascent").text = str(float(stats["total_ascent"]))
            if "total_descent" in stats:
                etree.SubElement(gpxtsxexts, GPXTSX + "Descent").text = str(float(stats["total_descent"]))
            if "total_calories" in stats:
                etree.SubElement(gpxtsxexts, GPXTSX + "Calories").text = str(float(stats["total_calories"]))
            if "avg_heart_rate" in stats:
                etree.SubElement(gpxtsxexts, GPXTSX + "AvgHeartRate").text = str(float(stats["avg_heart_rate"]))
            if "avg_cadence" in stats:
                etree.SubElement(gpxtsxexts, GPXTSX + "AvgCadence").text = str(float(stats["avg_cadence"]))

        def _calcStats(activity, session_stats):
            _mapStat(session_stats, "total_elapsed_time", (activity.EndTime - activity.StartTime).total_seconds())
            _mapStat(session_stats, "total_moving_time", activity.Stats.MovingTime.asUnits(ActivityStatisticUnit.Seconds).Value)
            _mapStat(session_stats, "total_stopped_time", session_stats["total_elapsed_time"] - session_stats["total_moving_time"])
            _mapStat(session_stats, "total_timer_time", activity.Stats.TimerTime.asUnits(ActivityStatisticUnit.Seconds).Value)
            _mapStat(session_stats, "total_distance", activity.Stats.Distance.asUnits(ActivityStatisticUnit.Meters).Value)
            _mapStat(session_stats, "total_calories", activity.Stats.Energy.asUnits(ActivityStatisticUnit.Kilocalories).Value)
            _mapStat(session_stats, "avg_speed", activity.Stats.Speed.asUnits(ActivityStatisticUnit.MetersPerSecond).Average)
            _mapStat(session_stats, "max_speed", activity.Stats.Speed.asUnits(ActivityStatisticUnit.MetersPerSecond).Max)
            _mapStat(session_stats, "avg_heart_rate", activity.Stats.HR.Average)
            _mapStat(session_stats, "max_heart_rate", activity.Stats.HR.Max)
            _mapStat(session_stats, "avg_cadence", _resolveRunCadence(activity.Stats.Cadence.Average, activity.Stats.RunCadence.Average))
            _mapStat(session_stats, "max_cadence", _resolveRunCadence(activity.Stats.Cadence.Max, activity.Stats.RunCadence.Max))
            _mapStat(session_stats, "avg_power", activity.Stats.Power.Average)
            _mapStat(session_stats, "max_power", activity.Stats.Power.Max)
            _mapStat(session_stats, "total_ascent", activity.Stats.Elevation.asUnits(ActivityStatisticUnit.Meters).Gain)
            _mapStat(session_stats, "total_descent", activity.Stats.Elevation.asUnits(ActivityStatisticUnit.Meters).Loss)
            _mapStat(session_stats, "avg_altitude", activity.Stats.Elevation.asUnits(ActivityStatisticUnit.Meters).Average)
            _mapStat(session_stats, "max_altitude", activity.Stats.Elevation.asUnits(ActivityStatisticUnit.Meters).Max)
            _mapStat(session_stats, "min_altitude", activity.Stats.Elevation.asUnits(ActivityStatisticUnit.Meters).Min)
            _mapStat(session_stats, "avg_temperature", activity.Stats.Temperature.asUnits(ActivityStatisticUnit.DegreesCelcius).Average)
            _mapStat(session_stats, "max_temperature", activity.Stats.Temperature.asUnits(ActivityStatisticUnit.DegreesCelcius).Max)

        session_stats = {}
        _calcStats(activity, session_stats)

        trk_extensions = etree.SubElement(trk, "extensions")
        _makeTrackStatsExtension(trk_extensions, session_stats)

        inPause = False
        for lap in activity.Laps:
            trkseg = etree.SubElement(trk, "trkseg")
            for wp in lap.Waypoints:
                if wp.Location is None or wp.Location.Latitude is None or wp.Location.Longitude is None:
                    continue  # drop the point
                if wp.Type == WaypointType.Pause:
                    if inPause:
                        continue  # this used to be an exception, but I don't think that was merited
                    inPause = True
                if inPause and wp.Type != WaypointType.Pause:
                    inPause = False
                trkpt = etree.SubElement(trkseg, "trkpt")
                if wp.Timestamp.tzinfo is None:
                    raise ValueError("GPX export requires TZ info")
                etree.SubElement(trkpt, "time").text = wp.Timestamp.astimezone(UTC).isoformat()
                trkpt.attrib["lat"] = str(wp.Location.Latitude)
                trkpt.attrib["lon"] = str(wp.Location.Longitude)
                if wp.Location.Altitude is not None:
                    etree.SubElement(trkpt, "ele").text = str(wp.Location.Altitude)
                if wp.HR is not None or wp.Cadence is not None or wp.Temp is not None or wp.Calories is not None or wp.Power is not None:
                    exts = etree.SubElement(trkpt, "extensions")
                    gpxtpxexts = etree.SubElement(exts, GPXTPX + "TrackPointExtension")
                    if wp.HR is not None:
                        etree.SubElement(gpxtpxexts, GPXTPX + "hr").text = str(int(wp.HR))
                    if wp.RunCadence is not None:
                        etree.SubElement(gpxtpxexts, GPXTPX + "cad").text = str(int(wp.RunCadence))
                    if wp.Cadence is not None:
                        etree.SubElement(gpxtpxexts, GPXTPX + "cad").text = str(int(wp.Cadence))
                    if wp.Temp is not None:
                        etree.SubElement(gpxtpxexts, GPXTPX + "atemp").text = str(wp.Temp)

            lap_stats = {}
            _calcStats(lap, lap_stats)

            trkseg_extensions = etree.SubElement(trkseg, "extensions")
            _makeTrackStatsExtension(trkseg_extensions, lap_stats)

        return etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="UTF-8").decode("UTF-8")
