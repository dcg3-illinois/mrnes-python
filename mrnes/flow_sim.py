import math
from typing import Dict, List, Optional
# import random
import net

# we will sometimes compare two float64s and if they are within eqlThrsh in value
# consider them to be equal
eqlThrsh = 1e-12

# represents a packet in a virtual stream, time of arrival at an interface,
# identity of the interface that sent it, and an identity of the message (for debugging)
class StrmPckt:
    def __init__(self, time: float, src_intrfc_id: int, msg_id: int):
        self.time: float = time
        self.src_intrfc_id: int = src_intrfc_id
        self.msg_id: int = msg_id

# represents the aggregate number of stream packets that arrive at a given time at an interface
class StrmArrivals:
    def __init__(self, time: float, number: int):
        self.time: float = time
        self.number: int = number

# strmPckt constructor
def create_strm_pckt(time: float, src_intrfc_id: int, msg_id: int) -> StrmPckt:
    # createStrmPckt
    return StrmPckt(time, src_intrfc_id, msg_id)

# a strmSet represents all the streams that pass through an interface.
class StrmSet:
    def __init__(self, iqs: net.intrfcQStruct, strm_rate: float, bndwdth: float, frame_len: int, rngstrm: random.Random):
        # iqs describes the queuing (for foreground packets and streams) at the interface
        self.iqs: net.intrfcQStruct = iqs
        
        # will be active only when there are streams assigned to pass through
        self.active: bool = False
        
        # time at which next packet arrival will occur at the strmSet, from any interface that serves as its source
        self.time: float = 0.0
        
        # bandwidth (in Mbps) of the interface
        self.bndwdth: float = bndwdth
        
        # each stream source to the strmSet is represented by a strmRep.
        # map is indexed by the source interface's ID
        self.strm_src_intrfc: Dict[int, StrmRep] = {}
        
        # frameLen is the number of bytes in a stream frame
        self.frame_len: int = frame_len
        
        frame_len_mbits = 8 * frame_len / 1e6

        # the rate (in frames/sec) that frames can be pushed through the interface
        self.service_rate: float = bndwdth / frame_len_mbits
        
        # time (in seconds) taken by a stream frame passing through the interface
        self.service_time: float = round_float(frame_len_mbits / bndwdth, rdigits)
        
        # cummulative probability distribution of the number of stream packets that arrive in a service time
        # slot, taken over all source interfaces
        self.jnt_arrivals_cdf: List[float] = []
        
        # cummulative probability distribution of the number of stream packets that arrive in a service time
        # slot, conditionned on excluding those from the interface whose id is the index
        self.cnd_arrivals_cdf: Dict[int, List[float]] = {}
        
        # number of concurrently arriving stream packets that are scheduled to be serviced _after_ the
        # foreground packet that also arrives concurrently is served
        self.fg_sample_residual = 0

        # the random number generator stream used by the strmSet
        self.rngstrm = rngstrm
        
        # list of arrivals in successive service time slots, generated and then simulated
        self.arrivals: List[StrmArrivals] = []

    # setBndwdth is called when the interface's bandwidth is set or changed
    def set_bndwdth(self, bndwdth: float):
        self.bndwdth = bndwdth
        frame_len_mbits = 8 * self.frame_len / 1e6
        self.service_rate = bndwdth / frame_len_mbits
        self.service_time = round_float(frame_len_mbits / bndwdth, rdigits)

    # createStrmArrivalsCDF is called after the stream reps for each of the
    # interface's source interfaces is known (in particular, the aggregate frame
    # rate directed to this interface). It creates a cummulative distribution function
    # for the number of stream packet arrivals that concurrently arrive at the interface
    # in one service time slot
    def create_strm_arrivals_cdf(self, src_intrfc_id: int) -> List[float]:
        # gather up the IDs of source interfaces that contribute streams (not including src_intrfc_id)
        strm_reps = [sid for sid in self.strm_src_intrfc if sid != src_intrfc_id]
        
        # presentPDF will hold the 'present' density function
        present_pdf = [0.0] * (len(strm_reps) + 1)
        
        # sampleSets is the number of unique combinations of a element of
        # strmReps arriving at the same time as the foreground flow
        sample_sets = 1 << len(strm_reps)
        
        # each integer between 0 and sampleSets-1 (inclusive) codes
        # a possible combination
        for set_idx in range(sample_sets):
            # for a given combination compute the probability that none of the
            # streams present an arrival simultaneously

            # for each member of strmReps see if its representation in the set code is '1'
            sets = 0
            pr_set = 1.0
            for rep in range(len(strm_reps)):
                
                # probability that strm packet arrives now.
                pr_present = self.strm_src_intrfc[strm_reps[rep]].strm_frame_rate / self.service_rate
                # create a bitmask
                mask = 1 << rep

                # non-zero if and only if strmReps[rep] is selected in the setIdx code
                if mask & set_idx:
                    # one more set
                    sets += 1

                    # the ratio of strmReps[rep]'s frame rate to the strmSet service rate is the probability of arriving,
                    # the probability that all the interfaces in the code do is the product
                    if(sets == 1):
                        pr_set = pr_present
                    else:
                        pr_set *= pr_present
                else:
                    if(sets == 1):
                        pr_set = (1.0 - pr_present)
                    else:
                        pr_set *= (1.0 - pr_present)

            # another way we get 'sets' simultaneous arrivals
            present_pdf[sets] += pr_set
        
        cdf = [0.0] * (len(strm_reps) + 1)
        cdf[0] = present_pdf[0]
        for idx in range(1, len(strm_reps)):
            cdf[idx] = cdf[idx-1] + present_pdf[idx]
        
        cdf[len(strm_reps)] = 1.0
        return cdf

    # sampleStrmArrivals randomly samples from either the joint
    # stream arrival distribution, or one conditioned on excluding one of the streams
    def sample_strm_arrivals(self, time: float, src_intrfc_id: int) -> StrmArrivals:
        if src_intrfc_id == 0:
            cdf = self.jnt_arrivals_cdf
            if not cdf:
                cdf = self.create_strm_arrivals_cdf(0)
                self.jnt_arrivals_cdf = cdf
        else:
            cdf = self.cnd_arrivals_cdf.get(src_intrfc_id)
            if cdf is None:
                cdf = self.create_strm_arrivals_cdf(src_intrfc_id)
                self.cnd_arrivals_cdf[src_intrfc_id] = cdf
        
        sa_time = time
        
        # use standard sampling technique of U01 with CDF.
	    # Assuming here that there are not so many source interfaces
	    # that we need to do a binary search
        u01 = self.rngstrm.random()
        number = 0
        for cnt in range(len(cdf)):
            if u01 < cdf[cnt]:
                number = cnt
                break
        return StrmArrivals(sa_time, number)

    # adjustStrmRate is called as a result of flow rate changes
    def adjust_strm_rate(self, src_intrfc_id: int, flow_id: int, strm_frame_rate: float):
        # see if this stream source is already present
        sr = self.strm_src_intrfc.get(src_intrfc_id)
        if sr:
            sr.adjust_strm_rate(flow_id, strm_frame_rate)
            if strm_frame_rate > 0.0:
                self.active = True
            return

        # not present so create one
        sr = create_strm_rep(src_intrfc_id, flow_id, strm_frame_rate / (self.frame_len * 8 / 1e6))
        sr.parent_set = self
        self.strm_src_intrfc[src_intrfc_id] = sr
        self.active = True

    # generateStrmArrivals is called when the waiting time for a foreground packet is to be computed.
    # samples arrivals up to time fgArrival (inclusive)
    def generate_strm_arrivals(self, fg_arrival: float, src_intrfc_id: int):
        self.arrivals = []

        # touch all the strm time slots up to but not including the foreward packt arrival time
        time = self.time
        while time < fg_arrival:

            # sample the number of arrivals at time from full joint distribution
            sa = self.sample_strm_arrivals(time, 0)

            # add it to the list to process
            self.arrivals.append(sa)

            # compute the time of the next strm event slot
            time = round_float(time + self.service_time, rdigits)

        # the sample for time fgarrival should exclude a stream from srcIntrfcID
        sa = self.sample_strm_arrivals(fg_arrival, src_intrfc_id)
        self.arrivals.append(sa)

    # queueingDelay returns a delay given to a foreground packet that arrives at
    def queueing_delay(self, current_time: float, fg_arrival: float, fg_service_time: float, src_intrfc_id: int) -> tuple[bool, float]:
        # queueingDelay returns a delay given to a foreground packet that arrives at
        # not active means nothing to do
        if not self.active:
            return False, 0.0

        # generate strm arrivals up to time fgArrival, with special attention to
        # the srcIntrfcID stream to reflect synchronization.  These are written into
        # ss.arrivals
        self.generate_strm_arrivals(fg_arrival, src_intrfc_id)

        # compute the waiting time derived from samples in ss.arrivals
        # using the formula q_n = max(0, q_{n-1}+a_n -1) where
        # a_n is the number of job arrivals at the nth step and q_j is the number
        # of packets in the system after the jth step

        # start with the residual from the last time queueingDelay was called
        qpckts = self.fg_sample_residual

        # reset
        self.fg_sample_residual = 0

        # compute the state of the stream queue upto the last time slot when there is
        # a foreground packet to process
        for idx in range(len(self.arrivals) - 1):
            sa = self.arrivals[idx]
            qpckts = max(0, qpckts + sa.number - 1)

        # knowing that there is a foreground packet to processing at this time slot
        # randomly select the number of strm packets also arriving then which
        # will precede it in receiving service
        sb = self.arrivals[-1]
        arrived = sb.number
        before = 0

        # sample the number of those concurrently 'arrived' packets
        if arrived > 0:
            # 'before' is the number that will precede the foreground pckt
            before = self.rngstrm.randint(0, arrived)
            # remember the residual
            self.fg_sample_residual = arrived - before

        # take ss.time up to the point where the foreground arrival will be processed
        # the 'arrived' variable is included so that if the last step does not process any
        # strm packets time does not advance and pass over the foreground packet
        last_arrival = self.arrivals[-1].time if self.arrivals else fg_arrival
        
        # bring ss.time up to the time when the foreground packet will be served
        self.time = round_float(last_arrival + self.service_time * (qpckts + before), rdigits)

        # prepare the outputs
        advance = round_float(self.time - current_time, rdigits)
        advanced = advance > 0.0

        # advance the strm simulator's time past serving the foreground pckt
        self.time = round_float(self.time + fg_service_time, rdigits)
        return advanced, advance

def create_strm_set(iqs: net.intrfcQStruct, strm_rate: float, bndwdth: float, frame_len: int, rngstrm: random.Random) -> StrmSet:
    # createStrmSet is a constructor
    ss = StrmSet(iqs, strm_rate, bndwdth, frame_len, rngstrm)
    return ss

# a strmRep represents an interface that sends stream packets to another.
# The recipient interface holds one of these structs for each of its sources
class StrmRep:
    def __init__(self, src_intrfc_id: int, flow_id: int, rate: float):
        self.parent_set: Optional[StrmSet] = None  # the strmSet that holds this representation
        self.strm_frame_rate_by_flow: Dict[int, float] = {flow_id: rate}  # indexed by flowID, gives the frames/sec associated with the flow
        self.src_intrfc_id = src_intrfc_id  # identity of the source interface
        self.active = True  # active is true when there is a strm from the source interface
        self.strm_frame_rate = rate  # aggregate rate of frames directed to the holding interface from the source
        self.service_time = 0.0  # time for passing a stream frame through the source interface
        self.nxt_arrival = 0.0
        self.arrivals: List[StrmPckt] = []

    # adjustStrmRate is called as a result of a flow rate changes
    def adjust_strm_rate(self, flow_id: int, strm_rate: float):
        if strm_rate > 0.0:
            self.active = True
        else:
            # strmRep goes inactive is the rate is zero
            self.active = False

        frame_len_mbits = 8 * self.parent_set.frame_len / 1e6 if self.parent_set else 1.0
        frame_rate = strm_rate / frame_len_mbits
        self.strm_frame_rate_by_flow[flow_id] = frame_rate
        self.strm_frame_rate = round_float(self.strm_frame_rate + frame_rate, rdigits)

# createStrmRep is a constructor
def create_strm_rep(src_intrfc_id: int, flow_id: int, rate: float) -> StrmRep:
    return StrmRep(src_intrfc_id, flow_id, rate)

rdigits = 15

# round computed simulation time to avoid non-sensical comparisons induced by rounding error
def round_float(val: float, precision: int) -> float:
    ratio = math.pow(10, float(precision))
    return round(val * ratio) / ratio

# expRV returns a sample of a exponentially distributed random number
def exp_rv(u01: float, rate: float) -> float:
    return -math.log(1.0 - u01) / rate

# sampleExpRV has the function signature expected by strmQ
# for calling a next interarrival time
def sample_exp_rv(u01: float, params: List[float]) -> float:
    return exp_rv(u01, params[0])

# sampleExpRV has the function signature expected by strmQ
# for calling a next interarrival time, here, a constant
def sample_const(u01: float, params: List[float]) -> float:
    return 1.0 / params[0]

def sample_geo_rv(u01: float, params: List[float]) -> float:
    hit_time = exp_rv(u01, params[0])
    trials = int(hit_time / params[1]) + 1
    return float(trials) * params[1]
