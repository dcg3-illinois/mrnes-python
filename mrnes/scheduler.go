package mrnes

// scheduler.go holds structs, methods and data structures that
// support scheduling of tasks, e.g., function executions, on resources
// that are limited

// When a task is scheduled the caller specifies how much service is required
// (in simulation time units), and a time-slice.   If the time-slice is larger the
// service, when given, is allocated all at once.   If the service requirement
// exceeds the time-slice the task is given the time-slice among of service, and the
// residual task is scheduled.    Allocation of core resources is first-come first-serve

import (
	_ "container/heap"
	"github.com/iti/evt/evtm"
	"github.com/iti/evt/vrtime"
	"math"
	"sort"
)

// Task describes the service requirements of an operation on a msg
type Task struct {
	OpType       string  // what operation is being performed
	arrive       float64 // time of task arrival
	req          float64 // required service
	pri          int     // priority, the larger the number the greater the priority
	timeslice    float64 // timeslice
	finish       bool    // flag that when the execution is complete the task has finished
	execID       int
	devID        int
	completeFunc evtm.EventHandlerFunction // call when finished
	context      any                       // remember this from caller, to return when finished
	Msg          any                       // information package being carried
}

// createTask is a constructor
func createTask(op string, arrive, req float64, pri int, timeslice float64, msg any, context any,
	execID, devID int, complete evtm.EventHandlerFunction) *Task {

	// if priority is zero, add 1
	if !(pri > 0) {
		return &Task{OpType: op, arrive: arrive, req: req, pri: 1,
			timeslice: timeslice, Msg: msg, context: context, execID: execID,
			devID: devID, completeFunc: complete}
	}
	return &Task{OpType: op, req: req, pri: pri, timeslice: timeslice, Msg: msg, context: context,
		execID: execID, devID: devID, completeFunc: complete}
}

// TaskScheduler holds data structures supporting the multi-core scheduling
type TaskScheduler struct {
	cores      int  // number of computational cores
	inBckgrnd  int  // number of cores being used for background traffic
	bckgrndOn  bool // set to true when background processing is in use
	waiting    map[int][]*Task
	numWaiting int   // total waiting, at all priority levels
	priorities []int // list of existing priorities, sorted in decreasing priority
	inservice  int   // manage work being served concurrently
}

// CreateTaskScheduler is a constructor
func CreateTaskScheduler(cores int) *TaskScheduler {
	ts := new(TaskScheduler)
	ts.cores = cores
	ts.waiting = make(map[int][]*Task)
	ts.priorities = make([]int, 0)
	ts.inservice = 0
	ts.numWaiting = 0
	return ts
}

// Schedule puts a piece of work either in queue to be done, or in service.  Parameters are
// - op : a code for the type of work being done
// - req : the service requirements for this task, on this computer
// - ts  : timeslice, the amount of service the task gets before yielding
// - msg : the message being processed
// - complete : an event handler to be called when the task has completed
// The return is true if the 'task is finished' event was scheduled.
func (ts *TaskScheduler) Schedule(evtMgr *evtm.EventManager, op string, req float64, pri int, timeslice float64,
	context any, msg any, execID, objID int, complete evtm.EventHandlerFunction) bool {

	AddSchedulerTrace(devTraceMgr, evtMgr.CurrentTime(), ts, execID, objID, "schedule["+op+"]")

	// create the Task, and remember it
	now := evtMgr.CurrentSeconds()
	task := createTask(op, now, req, pri, timeslice, msg, context, execID, objID, complete)

	// either put into service or put in the waiting queue
	executing := ts.joinQueue(evtMgr, task)

	// return flag indicating whether task was placed immediately into service
	return executing
}

// joinQueue is called to put a Task into the data structure that governs
// allocation of service
func (ts *TaskScheduler) joinQueue(evtMgr *evtm.EventManager, task *Task) bool {
	// if all the cores are busy, put in the waiting queue and return

	if ts.cores <= ts.inservice+ts.inBckgrnd {
		pri := task.pri
		_, present := ts.waiting[pri]

		if !present {
			ts.waiting[pri] = make([]*Task, 0)
			ts.priorities = append(ts.priorities, pri)
			if len(ts.priorities) > 1 {
				sort.Slice(ts.priorities, func(i, j int) bool { return ts.priorities[i] > ts.priorities[j] })
			}
		}
		ts.waiting[pri] = append(ts.waiting[pri], task)
		ts.numWaiting += 1

		// task.key = 1.0 / (math.Pow(now-task.arrive, 0.5) * task.pri)
		// heap.Push(&ts.priWaiting, task)
		return false
	}

	// execute the remaining required service time, or the timeslice, whichever is smaller
	var execute float64
	var finish bool
	if task.req <= task.timeslice {
		execute = task.req
		finish = true
	} else {
		execute = task.timeslice
		finish = false
	}
	task.finish = finish
	ts.inservice += 1

	// its main job is to pull the next job into service
	evtMgr.Schedule(ts, task, timeSliceComplete, vrtime.SecondsToTime(execute))

	// if it will have completed when finished, schedule the completion
	if finish {
		evtMgr.Schedule(task.context, task, task.completeFunc, vrtime.SecondsToTime(execute))
	}
	return finish
}

// timesliceComplete is called when the timeslice allocated to a task has completed
func timeSliceComplete(evtMgr *evtm.EventManager, context any, data any) any {
	ts := context.(*TaskScheduler)
	task := data.(*Task)
	ts.inservice -= 1

	ts.scheduleNxtTask(evtMgr)

	// if the task has not finished subtract off the timeslice and resubmit.
	if !task.finish {
		task.req -= task.timeslice
		ts.joinQueue(evtMgr, task)
	}
	return nil
}

func (ts *TaskScheduler) scheduleNxtTask(evtMgr *evtm.EventManager) bool {
	scheduled := false
	for _, pri := range ts.priorities {
		if len(ts.waiting[pri]) > 0 {
			task := ts.waiting[pri][0]
			ts.waiting[pri] = ts.waiting[pri][1:]
			ts.numWaiting -= 1
			ts.joinQueue(evtMgr, task)
			scheduled = true
			break
		}
	}
	return scheduled
}

// add a background task to a scheduler, give length of burst
func addBckgrnd(evtMgr *evtm.EventManager, context any, data any) any {
	endpt := context.(*endptDev)
	ts := endpt.EndptSched

	if !ts.bckgrndOn {
		return nil
	}

	// don't do anything if all the cores are busy
	if ts.inBckgrnd+ts.inservice < ts.cores {
		ts.inBckgrnd += 1

		u01 := u01List[endpt.EndptState.BckgrndIdx]
		endpt.EndptState.BckgrndIdx = (endpt.EndptState.BckgrndIdx + 1) % 10000

		duration := -endpt.EndptState.BckgrndSrv * math.Log(1.0-u01)
		// schedule the background task completion
		evtMgr.Schedule(endpt, nil, rmBckgrnd, vrtime.SecondsToTime(duration))
	}

	// schedule the next background arrival
	u01 := u01List[endpt.EndptState.BckgrndIdx]
	endpt.EndptState.BckgrndIdx = (endpt.EndptState.BckgrndIdx + 1) % 10000
	arrival := -math.Log(1.0-u01) / endpt.EndptState.BckgrndRate
	evtMgr.Schedule(endpt, nil, addBckgrnd, vrtime.SecondsToTime(arrival))
	return nil
}

// remove a background task
func rmBckgrnd(evtMgr *evtm.EventManager, context any, data any) any {
	endpt := context.(*endptDev)
	ts := endpt.EndptSched

	if !ts.bckgrndOn {
		return nil
	}

	if ts.inBckgrnd > 0 {
		ts.inBckgrnd -= 1
	}

	// if there are ordinary tasks in queue and enough cores now, free one up
	ts.scheduleNxtTask(evtMgr)
	return nil
}
