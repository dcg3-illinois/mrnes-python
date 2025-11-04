package mrnes

// flow-sim.go holds data structures and methods for
// the strmSet structure, used to do fast packet instant
// simulations of flows through interfaces
import (
	"github.com/iti/rngstream"
	"math"
	_ "sort"
)

// we will sometimes compare two float64s and if they are within eqlThrsh in value
// consider them to be equal
var eqlThrsh = 1e-12

// represents a packet in a virtual stream, time of arrival at an interface,
// identity of the interface that sent it, and an identity of the message (for debugging)
type strmPckt struct {
	time        float64
	srcIntrfcID int
	msgID       int
}

// represents the aggregate number of stream packets that arrive at a given time at an interface
type strmArrivals struct {
	time   float64
	number int
}

// strmPckt constructor
func createStrmPckt(time float64, srcIntrfcID int, msgID int) *strmPckt {
	sa := new(strmPckt)
	sa.time = time
	sa.srcIntrfcID = srcIntrfcID
	sa.msgID = msgID
	return sa
}

// a strmSet represents all the streams that pass through an interface.
type strmSet struct {
	// iqs describes the queuing (for foreground packets and streams) at the interface
	iqs *intrfcQStruct

	// will be active only when there are streams assigned to pass through
	active bool

	// time at which next packet arrival will occur at the strmSet, from any
	// interface that serves as its source
	time float64

	// bandwidth (in Mbps) of the interface
	bndwdth float64

	// each stream source to the strmSet is represented by a strmRep.
	// map is indexed by the source interface's ID
	strmSrcIntrfc map[int]*strmRep

	// frameLen is the number of bytes in a stream frame
	frameLen int

	// time (in seconds) taken by a stream frame passing through the interface
	serviceTime float64

	// the rate (in frames/sec) that frames can be pushed through the interface
	serviceRate float64

	// cummulative probability distribution of the number of stream packets that arrive in a service time
	// slot, taken over all source interfaces
	jntArrivalsCDF []float64

	// cummulative probability distribution of the number of stream packets that arrive in a service time
	// slot, conditionned on excluding those from the interface whose id is the index
	cndArrivalsCDF map[int][]float64

	// number of concurrently arriving stream packets that are scheduled to be serviced _after_ the
	// foreground packet that also arrives concurrently is served
	fgSampleResidual int

	// the random number generator stream used by the strmSet,
	rngstrm *rngstream.RngStream

	// list of arrivals in successive service time slots, generated and then simulated
	arrivals []strmArrivals
}

// createSet is a constructor for the strmSet
func createStrmSet(iqs *intrfcQStruct, strmRate, bndwdth float64, frameLen int,
	rngstrm *rngstream.RngStream) *strmSet {
	ss := new(strmSet)
	ss.arrivals = make([]strmArrivals, 0)
	ss.active = false
	ss.iqs = iqs
	ss.bndwdth = bndwdth
	ss.frameLen = frameLen
	frameLenMbits := float64(8*frameLen) / 1e6
	ss.serviceRate = bndwdth / frameLenMbits
	ss.serviceTime = roundFloat(frameLenMbits/bndwdth, rdigits)
	ss.rngstrm = rngstrm
	ss.strmSrcIntrfc = make(map[int]*strmRep)

	ss.jntArrivalsCDF = make([]float64, 0)
	ss.cndArrivalsCDF = make(map[int][]float64)

	return ss
}

// setBndwdth is called when the interface's bandwidth is set or changed
func (ss *strmSet) setBndwdth(bndwdth float64) {
	ss.bndwdth = bndwdth
	frameLenMbits := float64(8*ss.frameLen) / 1e6
	ss.serviceRate = bndwdth / frameLenMbits
	ss.serviceTime = roundFloat(frameLenMbits/bndwdth, rdigits)
}

// createStrmArrivalsCDF is called after the stream reps for each of the
// interface's source interfaces is known (in particular, the aggregate frame
// rate directed to this interface). It creates a cummulative distribution function
// for the number of stream packet arrivals that concurrently arrive at the interface
// in one service time slot
func (ss *strmSet) createStrmArrivalsCDF(srcIntrfcID int) []float64 {
	// gather up the IDs of source interfaces that contribute streams
	strmReps := []int{}

	// do not include the stream coming from interface srcIntrfcID
	for sid := range ss.strmSrcIntrfc {
		if sid == srcIntrfcID {
			continue
		}
		// remember the id of the strm source
		strmReps = append(strmReps, sid)
	}

	// presentPDF will hold the 'present' density function
	presentPDF := make([]float64, len(strmReps)+1)

	// sampleSets is the number of unique combinations of a element of
	// strmReps arriving at the same time as the foreground flow
	sampleSets := 1 << len(strmReps)

	// each integer between 0 and sampleSets-1 (inclusive) codes
	// a possible combination
	for setIdx := 0; setIdx < sampleSets; setIdx += 1 {

		// for a given combination compute the probability that none of the
		// streams present an arrival simultaneously

		// for each member of strmReps see if its representation in the set code is '1'
		var sets = 0
		var prSet = 1.0
		for rep := 0; rep < len(strmReps); rep++ {

			// probability that strm packet arrives now.
			// N.B. need to working in consideration of frequency at source
			prPresent := ss.strmSrcIntrfc[strmReps[rep]].strmFrameRate / ss.serviceRate
			// create a bitmask
			mask := 1 << rep

			// non-zero if and only if strmReps[rep] is selected in the setIdx code
			if mask&setIdx > 0 {
				// one more set
				sets += 1

				// the ratio of strmReps[rep]'s frame rate to the strmSet service rate is the probability of arriving,
				// the probability that all the interfaces in the code do is the product
				if sets == 1 {
					prSet = prPresent
				} else {
					prSet *= prPresent
				}
			} else {
				if sets == 1 {
					prSet = (1.0 - prPresent)
				} else {
					prSet *= (1.0 - prPresent)
				}
			}
		}
		// another way we get 'sets' simultaneous arrivals
		presentPDF[sets] += prSet
	}

	cdf := make([]float64, len(strmReps)+1)
	cdf[0] = presentPDF[0]
	for idx := 1; idx < len(strmReps); idx++ {
		cdf[idx] = cdf[idx-1] + presentPDF[idx]
	}
	cdf[len(strmReps)] = 1.0
	return cdf
}

// sampleStrmArrivals randomly samples from either the joint
// stream arrival distribution, or one conditioned on excluding one of the streams
func (ss *strmSet) sampleStrmArrivals(time float64, srcIntrfcID int) strmArrivals {
	var cdf []float64
	var present bool

	if srcIntrfcID == 0 {
		cdf = ss.jntArrivalsCDF
		if len(cdf) == 0 {
			// if the cdf doesn't exist yet, create it
			cdf = ss.createStrmArrivalsCDF(0)
		}
	} else {
		cdf, present = ss.cndArrivalsCDF[srcIntrfcID]
		if !present {
			// if the cdf doesn't exist yet, create it
			cdf = ss.createStrmArrivalsCDF(srcIntrfcID)
		}
	}

	// create the struct whose contents are returned
	sa := new(strmArrivals)

	sa.time = time // time is an input parameter

	// use standard sampling technique of U01 with CDF.
	// Assuming here that there are not so many source interfaces
	// that we need to do a binary search
	u01 := ss.rngstrm.RandU01()
	for cnt := 0; cnt < len(cdf); cnt += 1 {
		if u01 < cdf[cnt] {
			sa.number = cnt
			break
		}
	}
	return *sa
}

// adjustStrmRate is called as a result of flow rate changes
func (ss *strmSet) adjustStrmRate(srcIntrfcID int, flowID int, strmFrameRate float64) {
	// see if this stream source is already present
	sr, present := ss.strmSrcIntrfc[srcIntrfcID]
	if present {
		sr.adjustStrmRate(flowID, strmFrameRate)
		if strmFrameRate > 0.0 {
			ss.active = true
		}
		return
	}

	// not present so create one
	sr = createStrmRep(srcIntrfcID, flowID, strmFrameRate/(float64(ss.frameLen*8)/1e6))
	sr.parentSet = ss
	ss.strmSrcIntrfc[srcIntrfcID] = sr
	ss.active = true
}

// a strmRep represents an interface that sends stream packets to another.
// The recipient interface holds one of these structs for each of its sources
type strmRep struct {
	parentSet           *strmSet        // the strmSet that holds this representation
	strmFrameRateByFlow map[int]float64 // indexed by flowID, gives the frames/sec associated with the flow
	srcIntrfcID         int             // identity of the source interface
	active              bool            // active is true when there is a strm from the source interface
	strmFrameRate       float64         // aggregate rate of frames directed to the holding interface from the source
	serviceTime         float64         // time for passing a stream frame through the source interface
	nxtArrival          float64
	arrivals            []strmPckt
}

// createStrmRep is a constructor
func createStrmRep(srcIntrfcID int, flowID int, rate float64) *strmRep {
	sr := new(strmRep)
	sr.srcIntrfcID = srcIntrfcID
	sr.strmFrameRateByFlow = map[int]float64{flowID: rate}
	sr.strmFrameRate += rate
	sr.arrivals = make([]strmPckt, 0)
	sr.active = true
	return sr
}

// generateStrmArrivals is called when the waiting time for a foreground packet is to be computed.
// samples arrivals up to time fgArrival (inclusive)
func (ss *strmSet) generateStrmArrivals(fgArrival float64, srcIntrfcID int) {
	ss.arrivals = make([]strmArrivals, 0)

	var sa strmArrivals
	// touch all the strm time slots up to but not including the foreward pckt arrival time
	time := ss.time
	for time < fgArrival {

		// sample the number of arrivals at time from the full joint distribution
		sa = ss.sampleStrmArrivals(time, 0)

		// add it to the list to process
		ss.arrivals = append(ss.arrivals, sa)

		// compute the time of the nxt strm event slot
		time = roundFloat(time+ss.serviceTime, rdigits)
	}

	// the sample for time fgArrival should exclude a stream from srcIntrfcID
	sa = ss.sampleStrmArrivals(fgArrival, srcIntrfcID)
	ss.arrivals = append(ss.arrivals, sa)
}

// queueingDelay returns a delay given to a foreground packet that arrives at
func (ss *strmSet) queueingDelay(currentTime, fgArrival, fgServiceTime float64, srcIntrfcID int) (bool, float64) {
	// not active means nothing to do
	if !ss.active {
		return false, 0.0
	}

	// generate strm arrivals up to time fgArrival, with special attention to
	// the srcIntrfcID stream to reflect synchronization.  These are written into
	// ss.arrivals
	ss.generateStrmArrivals(fgArrival, srcIntrfcID)

	// compute the waiting time derived from samples in ss.arrivals
	// using the formula q_n = max(0, q_{n-1}+a_n -1) where
	// a_n is the number of job arrivals at the nth step and q_j is the number
	// of packets in the system after the jth step

	// start with the residual from the last time queueingDelay was called
	qpckts := ss.fgSampleResidual

	// reset
	ss.fgSampleResidual = 0

	// compute the state of the stream queue upto the last time slot when there is
	// a foreground packet to process
	for idx := 0; idx < len(ss.arrivals)-1; idx++ {
		sa := ss.arrivals[idx]
		qpckts = max(0, qpckts+sa.number-1)
	}

	// knowing that there is a foreground packet to processing at this time slot
	// randomly select the number of strm packets also arriving then which
	// will precede it in receiving service
	sb := ss.arrivals[len(ss.arrivals)-1]
	arrived := sb.number

	var before int

	// sample the number of those concurrently 'arrived' packets
	if arrived > 0 {

		// 'before' is the number that will precede the foreground pckt
		before = ss.rngstrm.RandInt(0, arrived)

		// remember the residual
		ss.fgSampleResidual = arrived - before
	}

	// take ss.time up to the point where the foreground arrival will be processed
	// the 'arrived' variable is included so that if the last step does not process any
	// strm packets time does not advance and pass over the foreground packet
	var lastArrival float64
	if len(ss.arrivals) > 0 {
		lastArrival = ss.arrivals[len(ss.arrivals)-1].time
	} else {
		lastArrival = fgArrival
	}

	// bring ss.time up to the time when the foreground packet will be served
	ss.time = roundFloat(lastArrival+ss.serviceTime*float64(qpckts+before), rdigits)

	// prepare the outputs
	advance := roundFloat(ss.time-currentTime, rdigits)
	advanced := (advance > 0.0)

	// advance the strm simulator's time past serving the foreground pckt
	ss.time = roundFloat(ss.time+fgServiceTime, rdigits)
	return advanced, advance
}

// adjustStrmRate is called as a result of a flow rate changes
func (sr *strmRep) adjustStrmRate(flowID int, strmRate float64) {
	if strmRate > 0.0 {
		sr.active = true
	} else {
		// strmRep goes inactive if the rate is zero
		sr.active = false
	}
	frameLenMbits := float64(8*sr.parentSet.frameLen) / 1e6
	frameRate := strmRate / frameLenMbits
	sr.strmFrameRateByFlow[flowID] = frameRate
	sr.strmFrameRate = roundFloat(sr.strmFrameRate+frameRate, rdigits)
}

var rdigits uint = 15

// round computed simulation time to avoid non-sensical comparisons
// induced by rounding error
func roundFloat(val float64, precision uint) float64 {
	ratio := math.Pow(10, float64(precision))
	return math.Round(val*ratio) / ratio
}

// expRV returns a sample of a exponentially distributed random number
func expRV(u01, rate float64) float64 {
	return -math.Log(1.0-u01) / rate
}

// sampleExpRV has the function signature expected by strmQ
// for calling a next interarrival time
func sampleExpRV(u01 float64, params []float64) float64 {
	return expRV(u01, params[0])
}

// sampleExpRV has the function signature expected by strmQ
// for calling a next interarrival time, here, a constant
func sampleConst(u01 float64, params []float64) float64 {
	return 1.0 / params[0]
}

func sampleGeoRV(u01 float64, params []float64) float64 {
	hitTime := expRV(u01, params[0])
	trials := int(hitTime/params[1]) + 1
	return float64(trials) * params[1]
}
