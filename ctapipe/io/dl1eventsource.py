import astropy.units as u
import logging
import numpy as np
import tables
from ctapipe.instrument import SubarrayDescription
from ctapipe.io.eventsource import EventSource
from ctapipe.io import HDF5TableReader
from ctapipe.io.datalevels import DataLevel
from ctapipe.containers import (
    ConcentrationContainer,
    EventAndMonDataContainer,
    EventIndexContainer,
    HillasParametersContainer,
    IntensityStatisticsContainer,
    LeakageContainer,
    MorphologyContainer,
    MCHeaderContainer,
    MCEventContainer,
    PeakTimeStatisticsContainer,
    TimingParametersContainer,
    TriggerContainer,
)


logger = logging.getLogger(__name__)


class DL1EventSource(EventSource):
    """
    Event source for files in the ctapipe DL1 format.
    For general information about the concept of event sources,
    take a look at the parent class ctapipe.io.EventSource.

    To use this event source, create an instance of this class
    specifying the file to be read.

    Looping over the EventSource yields events from the _generate_events
    method. An event equals an EventAndMonDataContainer instance.
    See ctapipe.containers.EventAndMonDataContainer for details.

    Attributes:
    -----------
    input_url: str
        Path to the input event file.
    file: tables.File
        File object
    obs_ids: list
        Observation ids of te recorded runs. For unmerged files, this
        should only contain a single number.
    subarray: ctapipe.instrument.SubarrayDescription
        The subarray configuration of the recorded run.
    datalevels: Tuple
        One or both of ctapipe.io.datalevels.DataLevel.DL1_IMAGES
        and ctapipe.io.datalevels.DataLevel.DL1_PARAMETERS
        depending on the information present in the file.
    is_simulation: Boolean
        Whether the events are simulated or observed.
    mc_headers: Dict
        Mapping of obs_id to ctapipe.containers.MCHeaderContainer
        if the file contains simulated events.
    has_simulated_dl1: Boolean
        Whether the file contains true camera images and/or
        image parameters evaluated on these.

    """

    def __init__(self, input_url, config=None, parent=None, **kwargs):
        """
        EventSource for dl1 files in the standard DL1 data format

        Parameters:
        -----------
        input_url : str
            Path of the file to load
        config : traitlets.loader.Config
            Configuration specified by config file or cmdline arguments.
            Used to set traitlet values.
            Set to None if no configuration to pass.
        parent:
            Parent from which the config is used. Mutually exclusive with config
        kwargs
        """
        super().__init__(input_url=input_url, config=config, parent=parent, **kwargs)

        self.file_ = tables.open_file(input_url)
        self.input_url = input_url
        self._subarray_info = SubarrayDescription.from_hdf(self.input_url)
        self._mc_headers = self._parse_mc_headers()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        self.file_.close()

    @staticmethod
    def is_compatible(file_path):
        with open(file_path, "rb") as f:
            magic_number = f.read(8)
        if magic_number != b"\x89HDF\r\n\x1a\n":
            return False
        with tables.open_file(file_path) as f:
            metadata = f.root._v_attrs
            if "CTA PRODUCT DESCRIPTION" not in metadata._v_attrnames:
                return False
            if metadata["CTA PRODUCT DESCRIPTION"] != "DL1 Data Product":
                return False
            if metadata["CTA PRODUCT DATA MODEL VERSION"] != "v1.0.0":
                return False
        return True

    @property
    def is_simulation(self):
        """
        True for files with a simulation group at the root of the file.
        """
        return "simulation" in self.file_.root

    @property
    def has_simulated_dl1(self):
        """
        True for files with telescope-wise event information in the simulation group
        """
        if self.is_simulation:
            if "telescope" in self.file_.root.simulation.event:
                return True
        return False

    @property
    def subarray(self):
        return self._subarray_info

    @property
    def datalevels(self):
        params = "parameters" in self.file_.root.dl1.event.telescope
        images = "images" in self.file_.root.dl1.event.telescope
        if params and images:
            return (DataLevel.DL1_IMAGES, DataLevel.DL1_PARAMETERS)
        elif params:
            return (DataLevel.DL1_PARAMETERS,)
        elif images:
            return (DataLevel.DL1_IMAGES,)

    @property
    def obs_ids(self):
        return list(np.unique(self.file_.root.dl1.event.subarray.trigger.col("obs_id")))

    @property
    def mc_headers(self):
        return self._mc_headers

    def _generator(self):
        yield from self._generate_events()

    def __len__(self):
        return len(self.file_.root.dl1.event.subarray.trigger)

    def _parse_mc_headers(self):
        """
        Construct a dict of MCHeaderContainers from the
        self.file_.root.configuration.simulation.run.
        These are used to match the correct header to each event
        """
        # Just returning next(reader) would work as long as there are no merged files
        # The reader ignores obs_id making the setup somewhat tricky
        # This is ugly but supports multiple headers so each event can have
        # the correct mcheader assigned by matching the obs_id
        # Alternatively this becomes a flat list
        # and the obs_id matching part needs to be done in _generate_events()
        mc_headers = {}
        if "simulation" in self.file_.root.configuration:
            reader = HDF5TableReader(self.file_).read(
                "/configuration/simulation/run", MCHeaderContainer()
            )
            row_iterator = self.file_.root.configuration.simulation.run.iterrows()
            for row in row_iterator:
                mc_headers[row["obs_id"]] = next(reader)
        return mc_headers

    def _generate_events(self):
        """
        Yield EventAndMonDataContainer to iterate through events.
        """
        data = EventAndMonDataContainer()
        # Maybe take some other metadata, but there are still some 'unknown'
        # written out by the stage1 tool
        data.meta["origin"] = self.file_.root._v_attrs["CTA PROCESS TYPE"]
        data.meta["input_url"] = self.input_url
        data.meta["max_events"] = self.max_events

        if DataLevel.DL1_IMAGES in self.datalevels:
            image_iterators = {
                tel.name: self.file_.root.dl1.event.telescope.images[
                    tel.name
                ].iterrows()
                for tel in self.file_.root.dl1.event.telescope.images
            }
            if self.has_simulated_dl1:
                true_image_iterators = {
                    tel.name: self.file_.root.simulation.event.telescope.images[
                        tel.name
                    ].iterrows()
                    for tel in self.file_.root.simulation.event.telescope.images
                }

        if DataLevel.DL1_PARAMETERS in self.datalevels:
            param_readers = {
                tel.name: HDF5TableReader(self.file_).read(
                    f"/dl1/event/telescope/parameters/{tel.name}",
                    containers=[
                        HillasParametersContainer(),
                        TimingParametersContainer(),
                        LeakageContainer(),
                        ConcentrationContainer(),
                        MorphologyContainer(),
                        IntensityStatisticsContainer(),
                        PeakTimeStatisticsContainer(),
                    ],
                    prefixes=True,
                )
                for tel in self.file_.root.dl1.event.telescope.parameters
            }
            if self.has_simulated_dl1:
                true_param_readers = {
                    tel.name: HDF5TableReader(self.file_).read(
                        f"/simulation/event/telescope/parameters/{tel.name}",
                        containers=[
                            HillasParametersContainer(),
                            TimingParametersContainer(),
                            LeakageContainer(),
                            ConcentrationContainer(),
                            MorphologyContainer(),
                            IntensityStatisticsContainer(),
                            PeakTimeStatisticsContainer(),
                        ],
                        prefixes=True,
                    )
                    for tel in self.file_.root.dl1.event.telescope.parameters
                }

        if self.is_simulation:
            # true shower wide information
            mc_shower_reader = HDF5TableReader(self.file_).read(
                "/simulation/event/subarray/shower", MCEventContainer(), prefixes="true"
            )

        # Setup iterators for the array events
        events = HDF5TableReader(self.file_).read(
            "/dl1/event/subarray/trigger", [TriggerContainer(), EventIndexContainer()]
        )
        for counter, array_event in enumerate(events):
            data.dl1.tel.clear()
            data.mc.tel.clear()
            data.pointing.tel.clear()
            data.trigger.tel.clear()

            data.count = counter
            data.trigger, data.index = next(events)
            data.trigger.tels_with_trigger = (
                np.where(data.trigger.tels_with_trigger)[0] + 1
            )  # +1 to match array index to telescope id

            # Maybe there is a simpler way  to do this
            # Beware: tels_with_trigger contains all triggered telescopes whereas
            # the telescope trigger table contains only the subset of
            # allowed_tels given during the creation of the dl1 file
            for i in self.file_.root.dl1.event.telescope.trigger.where(
                f"(obs_id=={data.index.obs_id}) & (event_id=={data.index.event_id})"
            ):
                if self.allowed_tels and i["tel_id"] not in self.allowed_tels:
                    continue
                data.trigger.tel[i["tel_id"]].time = i["telescopetrigger_time"]

            self._fill_array_pointing(data)
            self._fill_telescope_pointing(data)

            if self.is_simulation:
                data.mc = next(mc_shower_reader)
                data.mcheader = self._mc_headers[data.index.obs_id]

            for tel in data.trigger.tel.keys():
                if self.allowed_tels and tel not in self.allowed_tels:
                    continue
                dl1 = data.dl1.tel[tel]
                if DataLevel.DL1_IMAGES in self.datalevels:
                    if f"tel_{tel:03d}" not in image_iterators.keys():
                        logger.debug(
                            f"Triggered telescope {tel} is missing "
                            "from the image table."
                        )
                        continue
                    image_row = next(image_iterators[f"tel_{tel:03d}"])
                    dl1.image = image_row["image"]
                    dl1.peak_time = image_row["peak_time"]
                    dl1.image_mask = image_row["image_mask"]
                    # ToDo: Find a place in the data container, where the true images can go #1368

                if DataLevel.DL1_PARAMETERS in self.datalevels:
                    if f"tel_{tel:03d}" not in param_readers.keys():
                        logger.debug(
                            f"Triggered telescope {tel} is missing "
                            "from the parameters table."
                        )
                        continue
                    # Is there a smarter way to unpack this?
                    # Best would probbaly be if we could directly read
                    # into the ImageParametersContainer
                    params = next(param_readers[f"tel_{tel:03d}"])
                    dl1.parameters.hillas = params[0]
                    dl1.parameters.timing = params[1]
                    dl1.parameters.leakage = params[2]
                    dl1.parameters.concentration = params[3]
                    dl1.parameters.morphology = params[4]
                    dl1.parameters.intensity_statistics = params[5]
                    dl1.parameters.peak_time_statistics = params[6]
                    # ToDo: Find a place in the data container, where the true params can go #1368
            yield data

    def _fill_array_pointing(self, data):
        """
        Fill the array pointing information of a given event
        """
        # Only unique pointings are stored, so reader.read() wont work as easily
        # Not sure if this is the right way to do it
        # One could keep an index of the last selected row and the last pointing
        # and only advance with the row iterator if the next time
        # is closer or smth like that. But that would require peeking ahead
        closest_time = np.argmin(
            self.file_.root.dl1.monitoring.subarray.pointing.col("time")
            - data.trigger.time
        )
        array_pointing = self.file_.root.dl1.monitoring.subarray.pointing[closest_time]

        data.pointing.array_azimuth = u.Quantity(array_pointing["array_azimuth"], u.rad)
        data.pointing.array_altitude = u.Quantity(
            array_pointing["array_altitude"], u.rad
        )
        data.pointing.array_ra = u.Quantity(array_pointing["array_ra"], u.rad)
        data.pointing.array_dec = u.Quantity(array_pointing["array_dec"], u.rad)

    def _fill_telescope_pointing(self, data):
        """
        Fill the telescope pointing information of a given event
        """
        # Same comments as to _fill_array_pointing apply
        for tel in data.trigger.tel.keys():
            if self.allowed_tels and tel not in self.allowed_tels:
                continue
            tel_pointing_table = self.file_.root.dl1.monitoring.telescope.pointing[
                f"tel_{tel:03d}"
            ]
            closest_time = np.argmin(
                tel_pointing_table.col("telescopetrigger_time")
                - data.trigger.tel[tel].time
            )
            pointing_array = tel_pointing_table[closest_time]
            data.pointing.tel[tel].azimuth = u.Quantity(
                pointing_array["azimuth"], u.rad
            )
            data.pointing.tel[tel].altitude = u.Quantity(
                pointing_array["altitude"], u.rad
            )
