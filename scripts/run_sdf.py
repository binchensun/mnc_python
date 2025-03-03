#!/usr/bin/env python3

import os
import sys
import time
import numpy
import shutil
import argparse
import subprocess

import logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)-7s] %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)

import astropy.units as u
from astropy.time import Time, TimeDelta
from astropy.coordinates import SkyCoord, Angle, EarthLocation, AltAz
from astropy.coordinates import solar_system_ephemeris, get_body

from lwa_antpos.station import ovro

from mnc.mcs import Client as MCSClient
from mnc.common import LWATime, NCHAN as NCHAN_NATIVE, CLOCK as CLOCK_NATIVE
from mnc.xengine_beamformer_control import BeamPointingControl


# Beam tracking update control
#: Time step to use when determining beam pointings
_BEAM_UPDATE_STEP = TimeDelta(1, format='sec')
#: Maximum distance a source can move on the sky before a beam pointing update
_BEAM_UPDATE_DISTANCE = Angle('0:04:48', unit='deg') # This set based on LWA1's MCS
                                                     # step size of 0.2 degrees and
                                                     # scaling that to an aperature
                                                     # of 250 m.


def _tag_to_step(tag):
    """
    Convert a OBS_STP_* tag into a zero-based step index.
    """
    
    step = tag.split('[', 1)[1]
    step = int(step.split(']', 1)[0], 10) - 1
    return step


def _separation(aa0, aa1):
    """
    Wrapper around the astropy angular_separation function that takes in two
    AzAlt instances and returns the angular distance between them.
    
    From:
      https://github.com/astropy/astropy/blob/main/astropy/coordinates/angle_utilities.py
    """
    
    lon1, lat1 = aa0.az.rad, aa0.alt.rad
    lon2, lat2 = aa1.az.rad, aa1.alt.rad
    
    sdlon = numpy.sin(lon2 - lon1)
    cdlon = numpy.cos(lon2 - lon1)
    slat1 = numpy.sin(lat1)
    slat2 = numpy.sin(lat2)
    clat1 = numpy.cos(lat1)
    clat2 = numpy.cos(lat2)
    
    num1 = clat2 * sdlon
    num2 = clat1 * slat2 - slat1 * clat2 * cdlon
    denominator = slat1 * slat2 + clat1 * clat2 * cdlon
    
    return Angle(numpy.arctan2(numpy.hypot(num1, num2), denominator), unit='rad').to('deg')


def _get_tracking_updates(obs):
    """
    Given an observations dictionary, figure out when/how to update the beam
    pointing for the duration of the observation.  Returns a list of times/
    pointings (unix timestamp for the update, azimuth in degrees, elevation in
    degrees).
    """
    
    # Load the site
    site = EarthLocation.from_geocentric(*ovro.ecef, unit=u.m)
    
    # Load the start and stop times for this observation
    start = LWATime(obs['mjd'], obs['mpm']/1000/86400, format='mjd', scale='utc')
    stop =  LWATime(obs['mjd'], (obs['mpm']+obs['dur'])/1000/86400, format='mjd', scale='utc')
    
    # az/alt vs ra/dec step check - If we are in az/alt mode we only need to
    # point once at the beginning of the observation
    if obs['azalt']:
        return [[start.utc.unix, obs['ra'], obs['dec']],]
        
    # Load in the target - coordinates or a solar system body
    target_or_ra = obs['ra']
    dec = obs['dec']
    if dec is not None:
        ra = Angle(target_or_ra, unit='hourangle')
        dec = Angle(dec, unit='deg')
        sc = SkyCoord(ra, dec, frame='fk5')
    else:
        sc = get_body(target_or_ra.lower(), start+(stop-start)/2, location=site)
        
    # Figure out when to update based on how far the sources moves
    t = start
    updates = [t,]
    aa = sc.transform_to(AltAz(obstime=t, location=site))
    last_azalt = aa
    while t <= stop:
        aa = sc.transform_to(AltAz(obstime=t, location=site))
        if _separation(aa, last_azalt) >= _BEAM_UPDATE_DISTANCE:
            updates.append(t)
            last_azalt = aa
        t += _BEAM_UPDATE_STEP
    updates.append(stop)
    
    # Convert to updates into pointings that happen at t[i] but point to the
    # position of the source at the midpoint between t[i] and t[i+1].
    steps = []
    for i in range(len(updates)-1):
        t_diff = updates[i+1] - updates[i]
        t_step = updates[i] + t_diff / 2.0
        aa = sc.transform_to(AltAz(obstime=t_step, location=site))
        steps.append([updates[i].utc.unix, aa.az.deg, aa.alt.deg])
        
    # Done
    return steps


def _get_sdf_id(filename):
    pid, sid = None, None
    with open(filename, 'r') as fh:
        for line in fh:
            line = line.strip().rstrip()
            if len(line) < 3:
                continue
            if line[0] == '#':
                continue
                
            fields = line.split(None, 1)
            if fields[0] == 'PROJECT_ID':
                pid = fields[1].split('#')[0].strip().rstrip()
            elif fields[0] == 'SESSION_ID':
                sid = fields[1].split('#')[0].strip().rstrip()
                sid = int(sid, 10)
                
    return pid, sid


def parse_sdf(filename):
    """
    Given an SDF beamformer filename, parse the file and return a list of
    dictionaries that describe all of pointing and dwell times that we needed.
    
    The dictionaries contain:
     * 'mjd'   - the start time of the observation as an integer MJD
     * 'mpm'   - the start time of the observation as an integer ms past midnight
     * 'dur'   - the observation duration in ms
     * 'ra'    - the target RA in hours, J2000 -or- the Sun or Jupiter
     * 'dec'   - the target Dec in degrees, J2000 -or- None for the Sun/Jupiter
     * 'azalt' - if 'ra' and 'dec' are actually azimuth and elevation, both in
                 degrees
    """
    
    # Parse the SDF in a simple way that only keeps track of the pointing/durations
    obs = []
    beam = 1
    time_avg = 0
    stokes_mode = None
    tint_native = 2*NCHAN_NATIVE / CLOCK_NATIVE * 24
    with open(filename, 'r') as fh:
        for line in fh:
            line = line.strip().rstrip()
            
            if line.startswith('OBS_ID'):
                ## New observation?
                try:
                    obs.append(temp)
                except NameError:
                    pass
                temp = {'gain1': 6, 'gain2': 6}
            elif line.startswith('OBS_START_MJD'):
                ## Observation start MJD
                temp['mjd'] = int(line.rsplit(None, 1)[1], 10)
            elif line.startswith('OBS_START_MPM'):
                ## Observation start MPM
                temp['mpm'] = int(line.rsplit(None, 1)[1], 10)
            elif line.startswith('OBS_DUR') and not line.startswith('OBS_DUR+'):
                ## Observation duration in ms
                temp['dur'] = int(line.rsplit(None, 1)[1], 10)
            elif line.startswith('OBS_MODE'):
                ## Observation mode - this can fail if we are asked to do something
                ## we cannot.
                mode = line.rsplit(None, 1)[1]
                if mode == 'TRK_SOL':
                    ### Looking at the Sun is ok
                    temp['ra'] = 'sun'
                    temp['dec'] = None
                elif mode == 'TRK_JOV':
                    ### Looking at Jupiter is ok
                    temp['ra'] = 'jupiter'
                    temp['dec'] = None
                elif mode == 'TRK_LUN':
                    ### Looking at the Moon is ok
                    temp['ra'] = 'moon'
                    temp['dec'] = None
                elif mode in ('TRK_RADEC', 'STEPPED'):
                    ### Tracking a point on the sky is ok as is a stepped observation
                    ### so long as it is in RA/dec. (more on that later).
                    pass
                else:
                    raise RuntimeError(f"Invalid observing mode '{mode}'")
                temp['azalt'] = False
            elif line.startswith('OBS_RA'):
                ## RA to track in hours, J2000
                temp['ra'] = float(line.rsplit(None, 1)[1])
            elif line.startswith('OBS_DEC'):
                ## Dec. to track in degrees, J2000
                temp['dec'] = float(line.rsplit(None, 1)[1])
            elif line.startswith('OBS_FREQ1'):
                if line.find('+') == -1:
                    temp['freq1'] = float(line.rsplit(None, 1)[1]) / 2**32 * 196e6
            elif line.startswith('OBS_FREQ2'):
                if line.find('+') == -1:
                    temp['freq2'] = float(line.rsplit(None, 1)[1]) / 2**32 * 196e6
            elif line.startswith('OBS_STP_RADEC'):
                ## Stepped mode test - toggle azalt as needed
                mode = int(line.rsplit(None, 1)[1], 10)
                if mode != 1:
                    temp['azalt'] = True
            elif line.startswith('OBS_STP_N'):
                ## Stepped mode setup
                count = int(line.rsplit(None, 1)[1], 10)
                temp['steps'] = [{'ra':None, 'dec': None, 'dur':0, 'gain1':6, 'gain2':6} for i in range(count)]
            elif line.startswith('OBS_STP_C1'):
                ## Stepped mode ra in hours, J2000
                tag, ra = line.rsplit(None, 1)
                step = _tag_to_step(tag)
                ra = float(ra)
                for i in range(step, len(temp['steps'])):
                    temp['steps'][i]['ra'] = ra
            elif line.startswith('OBS_STP_C2'):
                ## Stepped mode dec. in degrees, J2000
                tag, dec = line.rsplit(None, 1)
                step = _tag_to_step(tag)
                dec = float(dec)
                for i in range(step, len(temp['steps'])):
                    temp['steps'][i]['dec'] = dec
            elif line.startswith('OBS_STP_T'):
                ## Stepped mode duration in ms
                tag, dur = line.rsplit(None, 1)
                step = _tag_to_step(tag)
                dur = int(dur, 10)
                for i in range(step, len(temp['steps'])):
                    temp['steps'][i]['dur'] = dur
            elif line.startswith('OBS_STP_FREQ1'):
                if line.find('+') == -1:
                    tag, freq1 = line.rsplit(None, 1)
                    step = _tag_to_step(tag)
                    freq1 = float(freq1) / 2**32 * 196e6
                    for i in range(step, len(temp['steps'])):
                        temp['steps'][i]['freq1'] = freq1
                        temp['steps'][i]['filter'] = active_bw_code
            elif line.startswith('OBS_STP_FREQ2'):
                if line.find('+') == -1:
                    tag, freq2 = line.rsplit(None, 1)
                    step = _tag_to_step(tag)
                    freq2 = float(freq2) / 2**32 * 196e6
                    for i in range(step, len(temp['steps'])):
                        temp['steps'][i]['freq2'] = freq2
                        temp['steps'][i]['filter'] = active_bw_code
            elif line.startswith('OBS_BW'):
                if line.find('+') == -1:
                    temp['filter'] = int(line.rsplit(None, 1)[1], 10)
                    active_bw_code = temp['filter']
                    
                    try:
                        for i in range(len(temp['steps'])):
                            temp['steps'][i]['filter'] = temp['filter']
                    except KeyError:
                        pass
            elif line.startswith('OBS_DRX_GAIN'):
                combined_gain = int(line.rsplit(None, 1)[1], 10)
                if combined_gain < 16:
                    temp['gain1'] = combined_gain
                    temp['gain2'] = combined_gain
                else:
                    temp['gain1'] = (combined_gain >> 4) & 0xF
                    temp['gain2'] = combined_gain & 0xF
            elif line.startswith('SESSION_DRX_BEAM'):
                beam = int(line.rsplit(None, 1)[1], 10)
            elif line.startswith('SESSION_SPC'):
                _, nchan, nwin = line.rsplit(None, 2)
                try:
                    nwin, stokes_mode = nwin.split('{')
                    stokes_mode = stokes_mode.split('=', 1)[1]
                    stokes_mode = stokes_mode.replace('}', '')
                except ValueError:
                    pass
                nchan = int(nchan, 10)
                nwin = int(nwin, 10)
                tint = (nchan*nwin / 19.6e6)
                time_avg = 2**int(round(numpy.log(tint / tint_native) / numpy.log(2)))
                time_avg = max([1, time_avg])
                time_avg = min([time_avg, 1024])
                
        # Add on that last observation
        obs.append(temp)
        
    # Expand the steps
    expanded_obs = []
    for o in obs:
        try:
            ## First step
            expanded_obs.append({'mjd':   o['mjd'],
                                 'mpm':   o['mpm'],
                                 'dur':   o['steps'][0]['dur'],
                                 'ra':    o['steps'][0]['ra'],
                                 'dec':   o['steps'][0]['dec'],
                                 'azalt': o['azalt'],
                                 'gain1': o['gain1'],
                                 'gain2': o['gain2']})
            try:
                expanded_obs[-1]['freq1']  = o['steps'][0]['freq1']
                expanded_obs[-1]['freq2']  = o['steps'][0]['freq2']
                expanded_obs[-1]['filter'] = o['steps'][0]['filter']
            except KeyError:
                pass
            ## Steps 2 through N so that the start time builds off the previous
            ## step's duration
            for s in o['steps'][1:]:
                expanded_obs.append({'mjd':   expanded_obs[-1]['mjd'],
                                     'mpm':   expanded_obs[-1]['mpm']+expanded_obs[-1]['dur'],
                                     'dur':   s['dur'],
                                     'ra':    s['ra'],
                                     'dec':   s['dec'],
                                     'azalt': expanded_obs[-1]['azalt'],
                                     'gain1': expanded_obs[-1]['gain1'],
                                     'gain2': expanded_obs[-1]['gain2']})
                try:
                    expanded_obs[-1]['freq1']  = s['freq1']
                    expanded_obs[-1]['freq2']  = s['freq2']
                    expanded_obs[-1]['filter'] = s['filter']
                except KeyError:
                    pass
                    
        except KeyError:
            ## Nope, it's a normal observation
            expanded_obs.append(o)
            
    # One last check and save the averaging time and beam
    if time_avg > 0:
        if round(tint, 3) != round(time_avg*tint_native, 3):
            logger.warn(f"Requested {tint*1000:.1f} ms spectrometer time resolution will not but used, {time_avg*tint_native*1000:.1f} ms will be used instead")
    elif beam != 1:
        time_avg = 1
        logger.warn(f"Spectrometer mode was not requested but will be used anyway with an integration time of {tint_native*1000:.1f} ms")
    else:
        logger.info("Running as a voltage beam observation")
    expanded_obs[0]['time_avg'] = time_avg
    expanded_obs[0]['stokes_mode'] = stokes_mode
    expanded_obs[0]['beam'] = beam
    
    return expanded_obs


def main(args):
    # Back to DEBUG now that we've imported everything
    logger.setLevel(logging.DEBUG)
    
    # Setup another log handler that writes to a file as a crude form of metadata
    obs_pid, obs_sid = _get_sdf_id(args.filename)
    metadata_name = "%s_%04i.history" % (obs_pid, obs_sid)
    metadata_handler = logging.FileHandler(metadata_name, mode='w')
    metadata_handler.setLevel(logging.DEBUG)
    metadata_formatter = logging.Formatter('%(asctime)s [%(levelname)-7s] %(message)s',
                                           datefmt='%Y-%m-%d %H:%M:%S')
    metadata_handler.setFormatter(metadata_formatter)
    logger.addHandler(metadata_handler)
    
    # Parse the SDF
    obs = parse_sdf(args.filename)
    logger.info(f"Loaded '{os.path.basename(args.filename)}' with a sequence of {len(obs)} pointing(s)")
    
    # Figure out the start and stop times for the actual run as well as when we
    # should start and stop the recording
    ## Observation
    start = LWATime(obs[0]['mjd'], obs[0]['mpm']/1000/86400, format='mjd', scale='utc')
    stop  = LWATime(obs[-1]['mjd'], (obs[-1]['mpm'] + obs[-1]['dur'])/1000/86400, format='mjd', scale='utc')
    dur = (stop - start).sec
    logger.info(f"Observations start at {start.datetime} and continue for {dur:.3f} s")
    
    ## Recording - start 5 s before and goes for 5 s afterwards
    rec_start = start - TimeDelta(5, format='sec')
    rec_stop = stop + TimeDelta(5, format='sec')
    rec_dur = (rec_stop - rec_start).sec
    logger.info(f"Recording starts at {rec_start.datetime} and continutes for {rec_dur:.3f} s")
    
    ## Validate
    if rec_start < LWATime.now() + TimeDelta(30, format='sec'):
        logger.error("Insufficient advanced notice to run this SDF, aborting")
        sys.exit(1)
    elif rec_start < LWATime.now():
        logger.error("SDF start time appears to be in the past, aborting")
        sys.exit(1)
        
    # Setup the beamformer stepping
    for o in obs:
        o['sdf_steps'] = _get_tracking_updates(o)
        for step in o['sdf_steps']:
            logger.debug(f"Beam to az {step[1]:.3f} deg, alt {step[2]:.3f} deg at {step[0]:.3f}")
            
    # Setup the control
    ## Recording
    try:
        dr = MCSClient()
    except Exception:
        logger.warn("Cannot create DR control object, will not send DR commands")
        dr = None
    ## Beamforming
    try:
        bf = BeamPointingControl(obs[0]['beam'])
    except Exception:
        logger.warn("Cannot create beamformer control object, will not send beamformer commands")
        bf = None
        
    # Recording
    ## Wait for the right time
    logger.info("Waiting for the recording time...")
    while LWATime.now() < rec_start - TimeDelta(15, format='sec'):
        time.sleep(0.01)
        
    ## Schedule it
    logger.info("Sending recorder command")
    if dr is not None and not args.dry_run:
        if obs[0]['beam'] == 1 and obs[0]['time_avg'] == 0:
            status = dr.send_command(f"drt{obs[0]['beam']}", 'record',
                                     beam=obs[0]['beam'],
                                     start_mjd=obs[0]['mjd'],
                                     start_mpm=obs[0]['mpm'],
                                     duration_ms=int(rec_dur*1000))
        else:
            status = dr.send_command(f"dr{obs[0]['beam']}", 'record',
                                     start_mjd=obs[0]['mjd'],
                                     start_mpm=obs[0]['mpm'],
                                     duration_ms=int(rec_dur*1000),
                                     time_avg=obs[0]['time_avg'],
                                     stokes_mode=obs[0]['stokes_mode'])
        if status[0]:
            logger.info("Record command succeeded: %s" % str(status[1:]))
            try:
                os.unlink("%s_%04i_metadata.txt" % (obs_pid, obs_sid))
            except OSError:
                pass
            with open("%s_%04i_metadata.txt" % (obs_pid, obs_sid), 'w') as fh:
                if status[1]['status'] == 'success':
                    fh.write("  1 [%s] ['%s']  0 [UNK]\n" % (os.path.basename(status[1]['response']['filename']),
                                                             os.path.dirname(status[1]['response']['filename'])))
        else:
            logger.error("Record command failed: %s", str(status[1]))
            
    # Beamforming/tracking
    ## Wait for the right time
    logger.info("Waiting for the start of the first observation...")
    while LWATime.now() + TimeDelta(1, format='sec') < start:
        time.sleep(0.01)
        
    ## Iterate through the observations
    last_freq1 = 0
    last_filter1 = -1
    last_gain1 = -1
    last_freq2 = 0
    last_filter2 = -1
    last_gain2 = -1
    for i,o in enumerate(obs):
        if obs[0]['beam'] == 1 and obs[0]['time_avg'] == 0:
            if o['freq1'] != last_freq1 \
               or o['filter'] != last_filter1 \
               or o['gain1'] != last_gain1:
                logger.info(f"Moving tuning 1 to {(o['freq1']/1e6):.3f} MHz, filter {o['filter']} at gain {o['gain1']}")
                if dr is not None and not args.dry_run:
                    dr.send_command(f"drt{obs[0]['beam']}", 'drx',
                                    beam=obs[0]['beam'],
                                    tuning=1,
                                    central_freq=o['freq1'],
                                    filter=o['filter'],
                                    gain=o['gain1'])
                last_freq1 = o['freq1']
                last_filter1 = o['filter']
                last_gain1 = o['gain1']
            if o['freq2'] != last_freq2 \
               or o['filter'] != last_filter2 \
               or o['gain2'] != last_gain2:
                logger.info(f"Moving tuning 2 to {(o['freq2']/1e6):.3f} MHz, filter {o['filter']} at gain {o['gain2']}")
                if dr is not None and not args.dry_run:
                    dr.send_command(f"drt{obs[0]['beam']}", 'drx',
                                    beam=obs[0]['beam'],
                                    tuning=2,
                                    central_freq=o['freq2'],
                                    filter=o['filter'],
                                    gain=o['gain2'])
                last_freq2 = o['freq2']
                last_filter2 = o['filter']
                last_gain2 = o['gain2']
                
        name = o['ra']
        if o['dec'] is not None:
            if not o['azalt']:
                name = f"{o['ra']} hr, {o['dec']} deg"
            else:
                name = f"{o['ra']} deg az, {o['dec']} deg el"
        logger.info(f"Tracking pointing #{i+1} ('{name}') for {o['dur']/1000.0:.3f} s")
        for step in o['sdf_steps']:
            while time.time() + 1 < step[0]:
                time.sleep(0.01)
            if bf is not None and not args.dry_run:
                bf.set_beam_pointing(step[1], step[2], degrees=True, load_time=step[0])
                
    # Close it out
    logger.info("Finished with the observations, waiting for the recording to finish...")
    while LWATime.now() < rec_stop:
        time.sleep(0.01)
        
    # Write a metadata tarball that contains the history and the SDF
    if not args.dry_run:
        ## History and SDF
        sdf_name = "%s_%04i.txt" % (obs_pid, obs_sid)
        sdf_copied = False
        if os.path.basename(args.filename) != sdf_name:
            shutil.copy(args.filename, sdf_name)
            sdf_copied = True
        to_include = [sdf_name, '%s_%04i.history' % (obs_pid, obs_sid)]
        ## Data file metdata
        if os.path.exists("%s_%04i_metadata.txt" % (obs_pid, obs_sid)):
            to_include.append("%s_%04i_metadata.txt" % (obs_pid, obs_sid))
        ## Try to also save the system configuration information
        try:
            os.unlink('system.config')
        except OSError:
            pass
        if dr is not None:
            try:
                config, _ = dr.client.get('/cfg/system')
                with open('system.config', 'wb') as fh:
                    fh.write(config)
                to_include.append('system.config')
            except Exception as e:
                logger.warning("Could not save system configuration: %s", str(e))
                
        ## Save
        cmd = ['tar', 'czf', '%s_%04i.tgz' % (obs_pid, obs_sid)]
        cmd.extend(to_include)
        subprocess.check_call(cmd)
        
        ## Cleanup
        if sdf_copied:
            try:
                os.unlink(sdf_name)
            except OSError:
                pass
        for name in cmd[4:]:
            try:
                os.unlink(name)
            except OSError:
                pass
                
    logger.info("Done")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='read in a SDF file and convert it to a sequence of commands to run',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
        )
    parser.add_argument('filename', type=str,
                        help='filename to parse and run')
    parser.add_argument('-n', '--dry-run', action='store_true',
                        help='parse and print commands but do not send them')
    args = parser.parse_args()
    main(args)
