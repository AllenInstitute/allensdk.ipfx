import re
import json

import h5py
import numpy as np
from pynwb import NWBHDF5IO
from pynwb.icephys import (CurrentClampSeries, CurrentClampStimulusSeries, VoltageClampSeries,
                           VoltageClampStimulusSeries, IZeroClampSeries)

import ipfx.stim_features as st


def get_long_unit_name(unit):
    if unit.startswith('A'):
        return "Amps"
    elif unit.startswith('V'):
        return "Volts"
    else:
        raise ValueError("Unit {} not recognized from TimeSeries".format(unit))


class NwbReader(object):

    def __init__(self, nwb_file, nwb_major_version):

        self.nwb_file = nwb_file
        self.nwb_major_version = nwb_major_version

        if self.nwb_major_version == 1:
            pass
        elif self.nwb_major_version == 2:
            self.acquisition_path = "acquisition"
            self.stimulus_path = "stimulus"
        else:
            raise ValueError("Unsupported NWB major version {}.".format(
                             nwb_major_version))

    def get_sweep_data(self):
        raise NotImplementedError

    def get_sweep_number(self):
        raise NotImplementedError

    def get_stim_code(self, sweep_name):
        raise NotImplementedError

    def get_sweep_attrs(self, sweep_name):

        with h5py.File(self.nwb_file, 'r') as f:
            sweep_ts = f["acquisition/timeseries"][sweep_name]
            attrs = dict(sweep_ts.attrs)

            if self.nwb_major_version == 2:
                for entry in sweep_ts.keys():
                    if entry in ("data", "electrode"):
                        continue

                    attrs[entry] = sweep_ts[entry].value

        return attrs

    def get_sweep_names(self):

        with h5py.File(self.nwb_file, 'r') as f:
            sweep_names = [e for e in f["acquisition/timeseries"].keys()]

        return sweep_names

    def get_pipeline_version(self):
        """ Returns the AI pipeline version number, stored in the
            metadata field 'generated_by'. If that field is
            missing, version 0.0 is returned.
            Borrowed from the AllenSDK

            Returns
            -------
            int tuple: (major, minor)
        """
        try:
            with h5py.File(self.nwb_file, 'r') as f:
                if 'generated_by' in f["general"]:
                    info = f["general/generated_by"]
                    # generated_by stores array of keys and values
                    # keys are even numbered, corresponding values are in
                    #   odd indices
                    for i in range(len(info)):
                        if info[i] == 'version':
                            version = info[i+1]
                            break
            toks = version.split('.')
            if len(toks) >= 2:
                major = int(toks[0])
                minor = int(toks[1])
        except:  # noqa: E722
            minor = 0
            major = 0
        return major, minor


class NwbXReader(NwbReader):
    """
    Read data from NWB v2 files created by run_x_to_nwb_conversion.py from
    ABF/DAT files.
    """

    def __init__(self, nwb_file):
        NwbReader.__init__(self, nwb_file, nwb_major_version=2)

    def get_sweep_number(self, sweep_name):
        return self.get_sweep_attrs(sweep_name)["sweep_number"]

    def get_stim_code(self, sweep_name):
        return self.get_sweep_attrs(sweep_name)["stimulus_description"]

    def get_sweep_data(self, sweep_number):
        """
        Parameters
        ----------
        sweep_number: int
        """

        if not isinstance(sweep_number, (int, np.uint64)):
            raise ValueError("sweep_number must be an integer")

        def getRawDataSourceType(experiment_description):
            """
            Return the original file format of the NWB file
            """

            d = {"abf": False, "dat": False, "unknown": False}

            if experiment_description.startswith("PatchMaster"):
                d["dat"] = True
            elif experiment_description.startswith("Clampex"):
                d["abf"] = True
            else:
                d["unknown"] = True

            return d

        with NWBHDF5IO(self.nwb_file, mode='r') as io:
            nwb = io.read()

            rawDataSourceType = getRawDataSourceType(nwb.experiment_description)
            assert not rawDataSourceType["unknown"], "Can handle data from this raw data source"

            series = nwb.sweep_table.get_series(sweep_number)

            if series is None:
                raise ValueError("No TimeSeries found for sweep number {}.".format(sweep_number))

            # we need one "*ClampStimulusSeries" and one "*ClampSeries"

            response = None
            stimulus = None

            for s in series:

                # ABF: Two channels, only the first has valid data
                # DAT: One channel
                if rawDataSourceType["abf"]:
                    description = json.loads(s.description)
                    channel_number = description['number']

                    if channel_number not in (1, 5):
                        raise ValueError("Unexpected channel number {} for TimeSeries {}.".format(channel_number, s))
                    elif channel_number == 5:
                        # this is the channel were the feedback stimulus is recorded
                        # TODO maybe we need a better way to find that channel??
                        continue

                if isinstance(s, (VoltageClampSeries, CurrentClampSeries, IZeroClampSeries)):
                    if response is not None:
                        raise ValueError("Found multiple response TimeSeries in NWB file for sweep number {}.".format(sweep_number))

                    response = s.data[:] * float(s.conversion)
                elif isinstance(s, (VoltageClampStimulusSeries, CurrentClampStimulusSeries)):
                    if stimulus is not None:
                        raise ValueError("Found multiple stimulus TimeSeries in NWB file for sweep number {}.".format(sweep_number))

                    stimulus = s.data[:] * float(s.conversion)
                    stimulus_unit = get_long_unit_name(s.unit)
                    stimulus_rate = float(s.rate)
                    stimulus_index_range = (0, int(s.num_samples - 1))
                else:
                    raise ValueError("Unexpected TimeSeries {}.".format(type(s)))

            if stimulus is None:
                raise ValueError("Could not find one stimulus TimeSeries for sweep number {}.".format(sweep_number))
            elif response is None:
                raise ValueError("Could not find one response TimeSeries for sweep number {}.".format(sweep_number))

            assert len(stimulus) == len(response), "Stimulus and response have a different length."

            return {
                'stimulus': stimulus,
                'response': response,
                'stimulus_unit': stimulus_unit,
                'index_range': stimulus_index_range,
                'sampling_rate': stimulus_rate
            }


class NwbPipelineReader(NwbReader):
    """
    Reads data from the NWB file generated by the ephys pipeline by converting
    the original NWB generated by MIES.
    """

    def __init__(self, nwb_file):
        NwbReader.__init__(self, nwb_file, nwb_major_version=1)

    def get_sweep_data(self, sweep_number):
        """
        Retrieve the stimulus, response, index_range, and sampling rate
        for a particular sweep.  This method hides the NWB file's distinction
        between a "Sweep" and an "Experiment".  An experiment is a subset of
        of a sweep that excludes the initial test pulse.  It also excludes
        any erroneous response data at the end of the sweep (usually for
        ramp sweeps, where recording was terminated mid-stimulus).
        Some sweeps do not have an experiment, so full data arrays are
        returned.  Sweeps that have an experiment return full data arrays
        (include the test pulse) with any erroneous data trimmed from the
        back of the sweep. Data is returned in mV and pA.
        Partially borrowed from the AllenSDK.get_sweep()

        Parameters
        ----------
        sweep_number: int

        Returns
        -------
        dict
            A dictionary with 'stimulus', 'response', 'index_range', and
            'sampling_rate' elements.  The index range is a 2-tuple where
            the first element indicates the end of the test pulse and the
            second index is the end of valid response data.
        """
        with h5py.File(self.nwb_file, 'r') as f:

            swp = f['epochs']['Sweep_%d' % sweep_number]

            #   fetch data from file and convert to correct SI unit
            #   this operation depends on file version. early versions of
            #   the file have incorrect conversion information embedded
            #   in the nwb file and data was stored in the appropriate
            #   SI unit. For those files, return uncorrected data.
            #   For newer files (1.1 and later), apply conversion value.

            stimulus_dataset = swp['stimulus']['timeseries']['data']
            stimulus_conversion = float(stimulus_dataset.attrs["conversion"])

            response_dataset = swp['response']['timeseries']['data']
            response_conversion = float(response_dataset.attrs["conversion"])

            major, minor = self.get_pipeline_version()
            if (major == 1 and minor > 0) or major > 1:
                stimulus = stimulus_dataset.value * stimulus_conversion
                response = response_dataset.value * response_conversion
            else:   # old file version
                stimulus = stimulus_dataset.value
                response = response_dataset.value

            if 'unit' in stimulus_dataset.attrs:
                unit = stimulus_dataset.attrs["unit"].decode('UTF-8')

                unit_str = get_long_unit_name(unit)
            else:
                unit = None
                unit_str = 'Unknown'

            if unit_str == "Amps":
                response *= 1e3,  # voltage, convert units V->mV
                stimulus *= 1e12,  # current, convert units A->pA

            elif unit_str == "Volts":
                response *= 1e12,  # current, convert units A->pA
                stimulus *= 1e3,  # voltage, convert units V->mV

            else:
                raise ValueError("Unknown stimulus unit")

            swp_idx_start = swp['stimulus']['idx_start'].value
            swp_length = swp['stimulus']['count'].value

            swp_idx_stop = swp_idx_start + swp_length - 1
            sweep_index_range = (swp_idx_start, swp_idx_stop)

            # if the sweep has an experiment, extract the experiment's index
            # range
            try:
                exp = f['epochs']['Experiment_%d' % sweep_number]
                exp_idx_start = exp['stimulus']['idx_start'].value
                exp_length = exp['stimulus']['count'].value
                exp_idx_stop = exp_idx_start + exp_length - 1
                index_range = (exp_idx_start, exp_idx_stop)
            except KeyError:
                # this sweep has no experiment.  return the index range of the
                # entire sweep.
                index_range = sweep_index_range

            assert sweep_index_range[0] == 0, Exception(
                "index range of the full sweep does not start at 0.")

            return {
                'stimulus': stimulus,
                'response': response,
                'stimulus_unit': unit_str,
                'index_range': index_range,
                'sampling_rate': 1.0 *
                swp['stimulus']['timeseries']['starting_time'].attrs['rate']
            }

    def get_sweep_number(self, sweep_name):

        sweep_number = int(sweep_name.split('_')[-1])
        return sweep_number

    def get_stim_code(self, sweep_name):

        stimulus_description = "aibs_stimulus_description"

        with h5py.File(self.nwb_file, 'r') as f:

            sweep_ts = f["acquisition/timeseries"][sweep_name]
            # look for the stimulus description
            if stimulus_description in sweep_ts.keys():
                stim_code_raw = sweep_ts[stimulus_description].value

                if type(stim_code_raw) is np.ndarray:
                    stim_code = str(stim_code_raw[0])
                else:
                    stim_code = str(stim_code_raw)

                if stim_code[-5:] == "_DA_0":
                    stim_code = stim_code[:-5]

        return stim_code


class NwbMiesReader(NwbReader):
    """
    Reads data from the MIES generated NWB file
    """

    def __init__(self, nwb_file):
        NwbReader.__init__(self, nwb_file, nwb_major_version=1)

    def get_sweep_data(self, sweep_number):

        with h5py.File(self.nwb_file, 'r') as f:
            sweep_response = f["acquisition/timeseries"]["data_%05d_AD0" % sweep_number]
            response = sweep_response["data"].value
            hz = 1.0 * sweep_response["starting_time"].attrs['rate']
            sweep_stimulus = f["stimulus/presentation"]["data_%05d_DA0" % sweep_number]
            stimulus = sweep_stimulus["data"].value

            if 'unit' in sweep_stimulus["data"].attrs:
                unit = sweep_stimulus["data"].attrs["unit"].decode('UTF-8')

                unit_str = None
                if unit.startswith('A'):
                    unit_str = "Amps"
                elif unit.startswith('V'):
                    unit_str = "Volts"
                assert unit_str is not None, Exception(
                    "Stimulus time series unit not recognized")
            else:
                unit_str = 'Unknown'

            if "CurrentClampSeries" in sweep_response.attrs["ancestry"]:
                index_range = st.get_experiment_epoch(stimulus, response, hz)
            elif "VoltageClampSeries" in sweep_response.attrs["ancestry"]:
                index_range = st.get_sweep_epoch(response)
            else:
                raise ValueError("Unknown Clamp Mode")

        return {"stimulus": stimulus,
                "response": response,
                "sampling_rate": hz,
                "stimulus_unit": unit_str,
                "index_range": index_range,
                }

    def get_sweep_number(self, sweep_name):

        sweep_number = int(sweep_name.split('_')[1])

        return sweep_number

    def get_stim_code(self, sweep_name):

        stimulus_description = "stimulus_description"

        with h5py.File(self.nwb_file, 'r') as f:

            sweep_ts = f["acquisition/timeseries"][sweep_name]
            # look for the stimulus description
            if stimulus_description in sweep_ts.keys():
                stim_code_raw = sweep_ts[stimulus_description].value

                if type(stim_code_raw) is np.ndarray:
                    stim_code = str(stim_code_raw[0])
                else:
                    stim_code = str(stim_code_raw)

                if stim_code[-5:] == "_DA_0":
                    stim_code = stim_code[:-5]

        return stim_code


def get_nwb_version(nwb_file):
    """
    Return a dict with `major` and `full` NWB version as read from the NWB file.
    """

    with h5py.File(nwb_file, 'r') as f:
        if "nwb_version" in f:         # In v1 this is a dataset
            nwb_version = f["nwb_version"].value
            if isinstance(nwb_version, np.ndarray):
                nwb_version = nwb_version[0]
            if nwb_version is not None and re.match("NWB-1", nwb_version):
                return {"major": 1, "full": nwb_version}

        elif "nwb_version" in f.attrs:   # but in V2 this is an attribute
            nwb_version = f.attrs["nwb_version"].value
            if nwb_version is not None and re.match("^2", nwb_version):
                return {"major": 2, "full": nwb_version}

    return {"major": None, "full": None}


def create_nwb_reader(nwb_file):
    """Create an appropriate reader of the nwb_file

    Parameters
    ----------
    nwb_file: str file name

    Returns
    -------
    reader object
    """

    nwb_version = get_nwb_version(nwb_file)

    if nwb_version["major"] == 2:
        return NwbXReader(nwb_file)
    elif nwb_version["major"] == 1:
        with h5py.File(nwb_file, 'r') as f:

            sweep_names = [e for e in f["acquisition/timeseries"].keys()]
            sweep_naming_convention = sweep_names[0].split('_')[0]

        if sweep_naming_convention == "data":
                return NwbMiesReader(nwb_file)
        elif sweep_naming_convention == "Sweep":
                return NwbPipelineReader(nwb_file)
        else:
            raise ValueError("Unknown sweep naming convention")
    else:
        raise ValueError("Unsupported or unknown NWB major" +
                         "version {} ({})".format(nwb_version["major"], nwb_version["full"]))
