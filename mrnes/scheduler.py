"""
scheduler.py holds classes, methods, and data structures that
support scheduling of tasks (e.g., function executions) on resources
that are limited.

When a task is scheduled the caller specifies how much service is required
(in simulation time units), and a time-slice. If the time-slice is larger than the
service, when given, it is allocated all at once. If the service requirement
exceeds the time-slice, the task is given the time-slice amount of service, and the
residual task is scheduled. Allocation of core resources is first-come first-serve.
"""
import math
import bisect
from collections import defaultdict
import evt_python.evt.vrtime as vrtime
import evt_python.evt.evtm as evtm
import mrnes.trace as trace

# Task describes the service requirements of an operation on a msg
class Task:
    def __init__(self, op, arrive, req, pri, timeslice, msg, context, execID, devID, completeFunc):
        self.OpType = op  # what operation is being performed
        self.arrive = arrive  # time of task arrival
        self.req = req  # required service
        if not (pri > 0):
            self.pri = 1  # priority, the larger the number the greater the priority
        else:
            self.pri = pri
        self.timeslice = timeslice  # timeslice
        self.finish = False  # flag that when the execution is complete the task has finished
        self.execID = execID
        self.devID = devID
        self.completeFunc = completeFunc  # call when finished
        self.context = context  # remember this from caller, to return when finished
        self.Msg = msg  # information package being carried


# TaskScheduler holds data structures supporting the multi-core scheduling
class TaskScheduler:
    def __init__(self, cores):
        self.cores = cores  # number of computational cores
        self.inBckgrnd = 0  # number of cores being used for background traffic
        self.bckgrndOn = False  # set to true when background processing is in use
        self.waiting = defaultdict(list)  # waiting queue by priority # TODO: not sure if I want this being defaultdict
        self.numWaiting = 0  # total waiting, at all priority levels
        self.priorities = []  # list of existing priorities, sorted in decreasing priority
        self.inservice = 0  # manage work being served concurrently


    # Schedule puts a piece of work either in queue to be done, or in service.
    # The return is True if the 'task is finished' event was scheduled.
    def schedule(self, ts, evtMgr, op, req, pri, timeslice, context, msg, execID, objID, complete):
        
        trace.AddSchedulerTrace(devTraceMgr, evtMgr.current_time(), ts, execID, objID, "schedule[{}]".format(op))
        
        # create the Task, and remember it
        now = evtMgr.current_seconds()
        task = Task(op, now, req, pri, timeslice, msg, context, execID, objID, complete)
        
        # either put into service or put in the waiting queue
        executing = self.join_queue(evtMgr, task)
        
        # return flag indicating whether task was placed immediately into service
        return executing

    # joinQueue is called to put a Task into the data structure that governs allocation of service
    def join_queue(self, evtMgr, task):
        # if all the cores are busy, put in the waiting queue and return
        if self.cores <= self.inservice + self.inBckgrnd:
            pri = task.pri

            # insert into the priorities list and keep sorted
            if pri not in self.waiting:
                bisect.insort(self.priorities, pri)
            
            self.waiting[pri].append(task)
            self.numWaiting += 1
            
            return False
        
        # execute the remaining required service time, or the timeslice, whichever is smaller
        if task.req <= task.timeslice:
            execute = task.req
            finish = True
        else:
            execute = task.timeslice
            finish = False

        task.finish = finish
        self.inservice += 1

        evtMgr.schedule(self, task, time_slice_complete, vrtime.seconds_to_time(execute))  # vrtime.SecondsToTime(execute) assumed
        
       	# if it will have completed when finished, schedule the completion
        if finish:
            evtMgr.schedule(task.context, task, task.completeFunc, vrtime.time_to_seconds(execute))
        return finish
    
    def schedule_nxt_task(self, evtMgr):
        scheduled = False
        for pri in sorted(self.priorities, reverse=True): # TODO: why is this reversed?
            if len(self.waiting[pri]) > 0:
                task = self.waiting[pri].pop(0)
                self.numWaiting -= 1
                self.join_queue(evtMgr, task)
                scheduled = True
                break
        return scheduled

# timesliceComplete is called when the timeslice allocated to a task has completed
def time_slice_complete(evtMgr: evtm.EventManager, context, data):
    ts = context  # type: TaskScheduler
    task = data  # type: Task
    ts.inservice -= 1

    ts.schedule_nxt_task(evtMgr)
    
    # if the task has not finished subtract off the timeslice and resubmit.
    if not task.finish:
        task.req -= task.timeslice
        ts.join_queue(evtMgr, task)

    return None

# add a background task to a scheduler, give length of burst
def add_bckgrnd(evtMgr, context, data):
    endpt = context # type: endptDev
    ts = endpt.EndptSched

    if not ts.bckgrndOn:
        return None
    
    # don't do anything if all the cores are busy
    if ts.inBckgrnd + ts.inservice < ts.cores:
        ts.inBckgrnd += 1
        
        u01 = u01List[endpt.EndptState.BckgrndIdx] # TODO: what is this u01 business?
        endpt.EndptState.BckgrndIdx = (endpt.EndptState.BckgrndIdx + 1) % 10000
        
        duration = -endpt.EndptState.BckgrndSrv * math.log(1.0 - u01)
        # schedule the background task completion
        evtMgr.schedule(endpt, None, rm_bckgrnd, vrtime.seconds_to_time(duration))

    # schedule the next background arrival
    u01 = u01List[endpt.EndptState.BckgrndIdx]
    endpt.EndptState.BckgrndIdx = (endpt.EndptState.BckgrndIdx + 1) % 10000
    arrival = -math.log(1.0 - u01) / endpt.EndptState.BckgrndRate
    evtMgr.schedule(endpt, None, add_bckgrnd, vrtime.seconds_to_time(arrival))
    return None

# remove a background task
def rm_bckgrnd(evtMgr, context, data):
    endpt = context  # type: endptDev
    ts = endpt.EndptSched

    if not ts.bckgrndOn:
        return None
    
    if ts.inBckgrnd > 0:
        ts.inBckgrnd -= 1

    # if there are ordinary tasks in queue and enough cores now, free one up
    ts.schedule_nxt_task(evtMgr)
    return None
