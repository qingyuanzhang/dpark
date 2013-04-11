import os, sys
import socket
import logging
import marshal
import cPickle
import threading, Queue
import time
import random
import getpass
import urllib
import warnings
import weakref
import multiprocessing

import zmq
try:
    import mesos, mesos_pb2
except ImportError:
    import pymesos as mesos
    import pymesos.mesos_pb2 as mesos_pb2

from util import compress, decompress, spawn
from dependency import NarrowDependency, ShuffleDependency 
from accumulator import Accumulator
from task import ResultTask, ShuffleMapTask
from job import SimpleJob
from env import env

logger = logging.getLogger("scheduler")

MAX_FAILED = 3
EXECUTOR_MEMORY = 64 # cache
POLL_TIMEOUT = 0.1
RESUBMIT_TIMEOUT = 60
MAX_IDLE_TIME = 60 * 30

class TaskEndReason: pass
class Success(TaskEndReason): pass
class FetchFailed:
    def __init__(self, serverUri, shuffleId, mapId, reduceId):
        self.serverUri = serverUri
        self.shuffleId = shuffleId
        self.mapId = mapId
        self.reduceId = reduceId
    def __str__(self):
        return '<FetchFailed(%s, %d, %d, %d)>' % (self.serverUri, 
                self.shuffleId, self.mapId, self.reduceId)

class OtherFailure:
    def __init__(self, message):
        self.message = message
    def __str__(self):
        return '<OtherFailure %s>' % self.message

class Stage:
    def __init__(self, rdd, shuffleDep, parents):
        self.id = self.newId()
        self.rdd = rdd
        self.shuffleDep = shuffleDep
        self.parents = parents
        self.numPartitions = len(rdd)
        self.outputLocs = [[] for i in range(self.numPartitions)]

    def __str__(self):
        return '<Stage(%d) for %s>' % (self.id, self.rdd)

    def __getstate__(self):
        raise Exception("should not pickle stage")

    @property
    def isAvailable(self):
        if not self.parents and self.shuffleDep == None:
            return True
        return all(self.outputLocs)

    def addOutputLoc(self, partition, host):
        self.outputLocs[partition].append(host)
        
#    def removeOutput(self, partition, host):
#        prev = self.outputLocs[partition]
#        self.outputLocs[partition] = [h for h in prev if h != host]
    
    nextId = 0
    @classmethod
    def newId(cls):
        cls.nextId += 1
        return cls.nextId


class Scheduler:
    def start(self): pass
    def runJob(self, rdd, func, partitions, allowLocal): pass
    def clear(self): pass
    def stop(self): pass
    def defaultParallelism(self):
        return 2

class CompletionEvent:
    def __init__(self, task, reason, result, accumUpdates):
        self.task = task
        self.reason = reason
        self.result = result
        self.accumUpdates = accumUpdates


class DAGScheduler(Scheduler):
    
    def __init__(self):
        self.completionEvents = Queue.Queue()
        self.idToStage = weakref.WeakValueDictionary()
        self.shuffleToMapStage = {}
        self.cacheLocs = {}
        self._shutdown = False
        self.keep_order = True

    def check(self):
        pass

    def clear(self):
        self.idToStage.clear()
        self.shuffleToMapStage.clear()
        self.cacheLocs.clear()
        self.cacheTracker.clear()
        self.mapOutputTracker.clear()

    def shutdown(self):
        self._shutdown = True

    @property
    def cacheTracker(self):
        return env.cacheTracker

    @property
    def mapOutputTracker(self):
        return env.mapOutputTracker

    def submitTasks(self, tasks):
        raise NotImplementedError

    def taskEnded(self, task, reason, result, accumUpdates):
        self.completionEvents.put(CompletionEvent(task, reason, result, accumUpdates))
    
    def getCacheLocs(self, rdd):
        return self.cacheLocs.get(rdd.id, [[] for i in range(len(rdd))])

    def updateCacheLocs(self):
        self.cacheLocs = self.cacheTracker.getLocationsSnapshot()

    def newStage(self, rdd, shuffleDep):
        stage = Stage(rdd, shuffleDep, self.getParentStages(rdd))
        self.idToStage[stage.id] = stage
        logger.debug("new stage: %s", stage) 
        return stage

    def getParentStages(self, rdd):
        parents = set()
        visited = set()
        def visit(r):
            if r.id in visited:
                return
            visited.add(r.id)
            if r.shouldCache:
                self.cacheTracker.registerRDD(r.id, len(r))
            for dep in r.dependencies:
                if isinstance(dep, ShuffleDependency):
                    parents.add(self.getShuffleMapStage(dep))
                else:
                    visit(dep.rdd)
        visit(rdd)
        return list(parents)

    def getShuffleMapStage(self, dep):
        stage = self.shuffleToMapStage.get(dep.shuffleId, None)
        if stage is None:
            stage = self.newStage(dep.rdd, dep)
            self.shuffleToMapStage[dep.shuffleId] = stage
        return stage

    def getMissingParentStages(self, stage):
        missing = set()
        visited = set()
        def visit(r):
            if r.id in visited:
                return
            visited.add(r.id)
            if r.shouldCache and all(self.getCacheLocs(r)):
                return

            for dep in r.dependencies:
                if isinstance(dep, ShuffleDependency):
                    stage = self.getShuffleMapStage(dep)
                    if not stage.isAvailable:
                        missing.add(stage)
                elif isinstance(dep, NarrowDependency):
                    visit(dep.rdd)

        visit(stage.rdd)
        return list(missing)

    def runJob(self, finalRdd, func, partitions, allowLocal):
        outputParts = list(partitions)
        numOutputParts = len(partitions)
        finalStage = self.newStage(finalRdd, None)
        results = [None]*numOutputParts
        finished = [None]*numOutputParts
        lastFinished = 0
        numFinished = 0

        waiting = set()
        running = set()
        failed = set()
        pendingTasks = {}
        lastFetchFailureTime = 0

        self.updateCacheLocs()
        
        logger.debug("Final stage: %s, %d", finalStage, numOutputParts)
        logger.debug("Parents of final stage: %s", finalStage.parents)
        logger.debug("Missing parents: %s", self.getMissingParentStages(finalStage))
      
        if allowLocal and (not finalStage.parents or not self.getMissingParentStages(finalStage)) and numOutputParts == 1:
            split = finalRdd.splits[outputParts[0]]
            yield func(finalRdd.iterator(split))
            return

        def submitStage(stage):
            logger.debug("submit stage %s", stage)
            if stage not in waiting and stage not in running:
                missing = self.getMissingParentStages(stage)
                if not missing:
                    submitMissingTasks(stage)
                    running.add(stage)
                else:
                    for parent in missing:
                        submitStage(parent)
                    waiting.add(stage)

        def submitMissingTasks(stage):
            myPending = pendingTasks.setdefault(stage, set())
            tasks = []
            have_prefer = True
            if stage == finalStage:
                for i in range(numOutputParts):
                    if not finished[i]:
                        part = outputParts[i]
                        if have_prefer:
                            locs = self.getPreferredLocs(finalRdd, part)
                            if not locs:
                                have_prefer = False
                        else:
                            locs = []
                        tasks.append(ResultTask(finalStage.id, finalRdd, 
                            func, part, locs, i))
            else:
                for p in range(stage.numPartitions):
                    if not stage.outputLocs[p]:
                        if have_prefer:
                            locs = self.getPreferredLocs(stage.rdd, p)
                            if not locs:
                                have_prefer = False
                        else:
                            locs = []
                        tasks.append(ShuffleMapTask(stage.id, stage.rdd,
                            stage.shuffleDep, p, locs))
            logger.debug("add to pending %s tasks", len(tasks))
            myPending |= set(t.id for t in tasks)
            self.submitTasks(tasks)

        submitStage(finalStage)

        while numFinished != numOutputParts:
            try:
                evt = self.completionEvents.get(False)
            except Queue.Empty:
                self.check()
                if self._shutdown:
                    sys.exit(1)

                if failed and time.time() > lastFetchFailureTime + RESUBMIT_TIMEOUT:
                    logger.debug("Resubmitting failed stages")
                    self.updateCacheLocs()
                    for stage in failed:
                        submitStage(stage)
                    failed.clear()
                else:
                    time.sleep(0.1)
                continue
               
            task = evt.task
            stage = self.idToStage[task.stageId]
            if stage not in pendingTasks: # stage from other job
                continue
            logger.debug("remove from pedding %s from %s", task, stage)
            pendingTasks[stage].remove(task.id)
            if isinstance(evt.reason, Success):
                Accumulator.merge(evt.accumUpdates)
                if isinstance(task, ResultTask):
                    finished[task.outputId] = True
                    numFinished += 1
                    if self.keep_order:
                        results[task.outputId] = evt.result
                        while lastFinished < numOutputParts and finished[lastFinished]:
                            yield results[lastFinished]
                            results[lastFinished] = None
                            lastFinished += 1
                    else:
                        yield evt.result

                elif isinstance(task, ShuffleMapTask):
                    stage = self.idToStage[task.stageId]
                    stage.addOutputLoc(task.partition, evt.result)
                    if not pendingTasks[stage]:
                        logger.debug("%s finished; looking for newly runnable stages", stage)
                        running.remove(stage)
                        if stage.shuffleDep != None:
                            self.mapOutputTracker.registerMapOutputs(
                                    stage.shuffleDep.shuffleId,
                                    [l[0] for l in stage.outputLocs])
                        self.updateCacheLocs()
                        newlyRunnable = set(stage for stage in waiting if not self.getMissingParentStages(stage))
                        waiting -= newlyRunnable
                        running |= newlyRunnable
                        logger.debug("newly runnable: %s, %s", waiting, newlyRunnable)
                        for stage in newlyRunnable:
                            submitMissingTasks(stage)
            else:
                logger.error("task %s failed", task)
                if isinstance(evt.reason, FetchFailed):
                    pass
                else:
                    logger.error("%s %s %s", evt.reason, type(evt.reason), evt.reason.message)
                    raise evt.reason

        assert not any(results)
        return

    def getPreferredLocs(self, rdd, partition):
        return rdd.preferredLocations(rdd.splits[partition])


def run_task(task, aid):
    logger.debug("Running task %r", task)
    try:
        Accumulator.clear()
        result = task.run(aid)
        accumUpdates = Accumulator.values()
        return (task.id, Success(), result, accumUpdates)
    except Exception, e:
        logger.error("error in task %s", task)
        import traceback
        traceback.print_exc()
        return (task.id, OtherFailure("exception:" + str(e)), None, None)


class LocalScheduler(DAGScheduler):
    attemptId = 0
    def nextAttempId(self):
        self.attemptId += 1
        return self.attemptId

    def submitTasks(self, tasks):
        logger.debug("submit tasks %s in LocalScheduler", tasks)
        for task in tasks:
#            task = cPickle.loads(cPickle.dumps(task, -1))
            _, reason, result, update = run_task(task, self.nextAttempId())
            self.taskEnded(task, reason, result, update)

def run_task_in_process(task, tid, environ):
    from env import env
    env.start(False, environ)
    
    logger.debug("run task in process %s %s", task, tid)
    try:
        return run_task(task, tid)
    except KeyboardInterrupt:
        sys.exit(0)

class MultiProcessScheduler(LocalScheduler):
    def __init__(self, threads):
        LocalScheduler.__init__(self)
        self.threads = threads
        self.tasks = {}
        self.pool = multiprocessing.Pool(self.threads or 2)

    def submitTasks(self, tasks):
        logger.info("Got a job with %d tasks", len(tasks))
        
        total, self.finished, start = len(tasks), 0, time.time()
        def callback(args):
            logger.debug("got answer: %s", args)
            tid, reason, result, update = args
            task = self.tasks.pop(tid)
            self.finished += 1
            logger.info("Task %s finished (%d/%d)        \x1b[1A",
                tid, self.finished, total)
            if self.finished == total:
                logger.info("Job finished in %.1f seconds" + " "*20,  time.time() - start)
            self.taskEnded(task, reason, result, update)
        
        for task in tasks:
            logger.debug("put task async: %s", task)
            self.tasks[task.id] = task
            self.pool.apply_async(run_task_in_process,
                [task, self.nextAttempId(), env.environ],
                callback=callback)

    def stop(self):
        self.pool.terminate()
        self.pool.join()
        logger.debug("process pool stopped")


def profile(f):
    def func(*args, **kwargs):
        path = '/tmp/worker-%s.prof' % os.getpid()
        import cProfile
        import pstats
        func = f
        cProfile.runctx('func(*args, **kwargs)', 
            globals(), locals(), path)
        stats = pstats.Stats(path)
        stats.strip_dirs()
        stats.sort_stats('time', 'calls')
        stats.print_stats(20)
        stats.sort_stats('cumulative')
        stats.print_stats(20)
    return func

def safe(f):
    def _(self, *a, **kw):
        with self.lock:
            r = f(self, *a, **kw)
        return r
    return _

def int2ip(n):
    return "%d.%d.%d.%d" % (n & 0xff, (n>>8)&0xff, (n>>16)&0xff, n>>24)

class MesosScheduler(DAGScheduler):

    def __init__(self, master, options):
        DAGScheduler.__init__(self)
        self.master = master
        self.keep_order = options.keep_order
        self.use_self_as_exec = options.self
        self.cpus = options.cpus
        self.mem = options.mem
        self.task_per_node = options.parallel or 8
        self.group = options.group
        self.logLevel = options.logLevel
        self.options = options
        self.started = False
        self.last_finish_time = 0
        self.isRegistered = False
        self.executor = None
        self.driver = None
        self.out_logger = None
        self.err_logger = None
        self.lock = threading.RLock()
        self.init_job()

    def init_job(self):
        self.activeJobs = {}
        self.activeJobsQueue = []
        self.taskIdToJobId = {}
        self.taskIdToSlaveId = {}
        self.jobTasks = {}
        self.slaveTasks = {}
        self.slaveFailed = {}

    def clear(self):
        DAGScheduler.clear(self)
        self.init_job()

    def start(self):
        if not self.out_logger:
            self.out_logger = self.start_logger(sys.stdout) 
        if not self.err_logger:
            self.err_logger = self.start_logger(sys.stderr)

    def start_driver(self):
        name = '[dpark@%s] ' % socket.gethostname()
        name += os.path.abspath(sys.argv[0]) + ' ' + ' '.join(sys.argv[1:])
        framework = mesos_pb2.FrameworkInfo()
        framework.user = getpass.getuser()
        if framework.user == 'root':
            raise Exception("dpark is not allowed to run as 'root'")
        framework.name = name

        # ignore INFO and DEBUG log
        os.environ['GLOG_logtostderr'] = '1'
        os.environ['GLOG_minloglevel'] = '1'
        self.driver = mesos.MesosSchedulerDriver(self, framework,
                                                 self.master)
        self.driver.start()
        logger.debug("Mesos Scheudler driver started")

        self.started = True
        self.last_finish_time = time.time()
        def check():
            while self.started:
                now = time.time()
                if not self.activeJobs and now - self.last_finish_time > MAX_IDLE_TIME:
                    logger.info("stop mesos scheduler after %d seconds idle", 
                            now - self.last_finish_time)
                    self.stop()
                    break
                time.sleep(1)
        
        spawn(check)

    def start_logger(self, output):
        sock = env.ctx.socket(zmq.PULL)
        port = sock.bind_to_random_port("tcp://0.0.0.0")

        def collect_log():
            while True:
                line = sock.recv()
                output.write(line)

        spawn(collect_log)

        host = socket.gethostname()
        addr = "tcp://%s:%d" % (host, port)
        logger.debug("log collecter start at %s", addr)
        return addr

    @safe
    def registered(self, driver, frameworkId, masterInfo):
        self.isRegistered = True
        logger.debug("connect to master %s:%s(%s), registered as %s", 
            int2ip(masterInfo.ip), masterInfo.port, masterInfo.id, 
            frameworkId.value)
        self.executor = self.getExecutorInfo(str(frameworkId.value))

    @safe
    def reregistered(self, driver, masterInfo):
        logger.debug("framework is reregistered")

    @safe
    def disconnected(self, driver):
        logger.debug("framework is disconnected")

    @safe
    def executorlost(self, driver, executorId, slaveId, status):
        logger.warning("executor %s is lost at %s", executorId.value, slaveId.value)

    @safe
    def getExecutorInfo(self, framework_id):
        info = mesos_pb2.ExecutorInfo()
        if hasattr(info, 'framework_id'):
            info.framework_id.value = framework_id

        if self.use_self_as_exec:
            info.command.value = os.path.abspath(sys.argv[0])
            info.executor_id.value = sys.argv[0]
        else:
            dir = os.path.dirname(__file__)
            info.command.value = os.path.abspath(os.path.join(dir, 'bin/executor%d%d.py' % sys.version_info[:2]))
            info.executor_id.value = "default"
        
        mem = info.resources.add()
        mem.name = 'mem'
        mem.type = 0 #mesos_pb2.Value.SCALAR
        mem.scalar.value = EXECUTOR_MEMORY
        info.data = marshal.dumps((os.path.realpath(sys.argv[0]), os.getcwd(), sys.path, dict(os.environ),
            self.task_per_node, self.out_logger, self.err_logger, self.logLevel, env.environ))
        return info

    @safe
    def submitTasks(self, tasks):
        if not tasks:
            return

        job = SimpleJob(self, tasks, self.cpus, tasks[0].rdd.mem or self.mem)
        self.activeJobs[job.id] = job
        self.activeJobsQueue.append(job)
        self.jobTasks[job.id] = set()
        logger.info("Got job %d with %d tasks", job.id, len(tasks))
      
        need_revive = self.started
        if not self.started:
            self.start_driver()
        while not self.isRegistered:
            self.lock.release()
            time.sleep(0.01)
            self.lock.acquire()
        
        if need_revive:
            self.requestMoreResources()

    def requestMoreResources(self): 
        logger.debug("reviveOffers")
        self.driver.reviveOffers()

    @safe
    def resourceOffers(self, driver, offers):
        rf = mesos_pb2.Filters()
        if not self.activeJobs:
            rf.refuse_seconds = 60 * 5
            for o in offers:
                driver.launchTasks(o.id, [], rf)
            return

        start = time.time()
        random.shuffle(offers)
        cpus = [self.getResource(o.resources, 'cpus') for o in offers]
        mems = [self.getResource(o.resources, 'mem') 
                - (o.slave_id.value not in self.slaveTasks
                    and EXECUTOR_MEMORY or 0)
                for o in offers]
        logger.debug("get %d offers (%s cpus, %s mem), %d jobs", 
            len(offers), sum(cpus), sum(mems), len(self.activeJobs))

        tasks = {}
        for job in self.activeJobsQueue:
            while True:
                launchedTask = False
                for i,o in enumerate(offers):
                    sid = o.slave_id.value
                    if self.group and (self.getAttribute(o.attributes, 'group') or 'none') not in self.group:
                        continue
                    if self.slaveFailed.get(sid, 0) >= MAX_FAILED:
                        continue
                    if self.slaveTasks.get(sid, 0) >= self.task_per_node:
                        continue
                    if mems[i] < self.mem or cpus[i]+1e-4 < self.cpus:
                        continue
                    t = job.slaveOffer(str(o.hostname), cpus[i], mems[i])
                    if not t:
                        continue
                    task = self.createTask(o, job, t, cpus[i])
                    tasks.setdefault(o.id.value, []).append(task)

                    logger.debug("dispatch %s into %s", t, o.hostname)
                    tid = task.task_id.value
                    self.jobTasks[job.id].add(tid)
                    self.taskIdToJobId[tid] = job.id
                    self.taskIdToSlaveId[tid] = sid
                    self.slaveTasks[sid] = self.slaveTasks.get(sid, 0)  + 1 
                    cpus[i] -= min(cpus[i], t.cpus)
                    mems[i] -= t.mem
                    launchedTask = True

                if not launchedTask:
                    break
        
        used = time.time() - start
        if used > 10:
            logger.error("use too much time in slaveOffer: %.2fs", used)

        rf.refuse_seconds = 5
        for o in offers:
            driver.launchTasks(o.id, tasks.get(o.id.value, []), rf)

        logger.debug("reply with %d tasks, %s cpus %s mem left", 
            sum(len(ts) for ts in tasks.values()), sum(cpus), sum(mems))
   
    @safe
    def offerRescinded(self, driver, offer_id):
        logger.debug("rescinded offer: %s", offer_id)
        if self.activeJobs:
            self.requestMoreResources()

    def getResource(self, res, name):
        for r in res:
            if r.name == name:
                return r.scalar.value

    def getAttribute(self, attrs, name):
        for r in attrs:
            if r.name == name:
                return r.text.value

    def createTask(self, o, job, t, available_cpus):
        task = mesos_pb2.TaskInfo()
        tid = "%s:%s:%s" % (job.id, t.id, t.tried)
        task.name = "task %s" % tid
        task.task_id.value = tid
        task.slave_id.value = o.slave_id.value
        task.data = compress(cPickle.dumps((t, t.tried), -1))
        task.executor.MergeFrom(self.executor)
        if len(task.data) > 1000*1024:
            logger.warning("task too large: %s %d", 
                t, len(task.data))

        cpu = task.resources.add()
        cpu.name = 'cpus'
        cpu.type = 0 #mesos_pb2.Value.SCALAR
        cpu.scalar.value = min(t.cpus, available_cpus)
        mem = task.resources.add()
        mem.name = 'mem'
        mem.type = 0 #mesos_pb2.Value.SCALAR
        mem.scalar.value = t.mem
        return task

    @safe
    def statusUpdate(self, driver, status):
        tid = status.task_id.value
        state = status.state
        logger.debug("status update: %s %s", tid, state)

        jid = self.taskIdToJobId.get(tid)
        if jid not in self.activeJobs:
            logger.debug("Ignoring update from TID %s " +
                "because its job is gone", tid)
            return

        job = self.activeJobs[jid]
        _, task_id, tried = map(int, tid.split(':'))
        if state == mesos_pb2.TASK_RUNNING:
            return job.statusUpdate(task_id, tried, state)

        del self.taskIdToJobId[tid]
        self.jobTasks[jid].remove(tid)
        slave_id = self.taskIdToSlaveId[tid]
        if slave_id in self.slaveTasks:
            self.slaveTasks[slave_id] -= 1
        del self.taskIdToSlaveId[tid]
   
        if state in (mesos_pb2.TASK_FINISHED, mesos_pb2.TASK_FAILED) and status.data:
            try:
                task_id,reason,result,accUpdate = cPickle.loads(status.data)
                if result:
                    flag, data = result
                    if flag >= 2:
                        try:
                            data = urllib.urlopen(data).read()
                        except IOError:
                            # try again
                            data = urllib.urlopen(data).read()
                        flag -= 2
                    data = decompress(data)
                    if flag == 0:
                        result = marshal.loads(data)
                    else:
                        result = cPickle.loads(data)
                return job.statusUpdate(task_id, tried, state, 
                    reason, result, accUpdate)
            except Exception, e:
                logger.warning("error when cPickle.loads(): %s, data:%s", e, len(status.data))
                state = mesos_pb2.TASK_FAILED
                return job.statusUpdate(task_id, tried, mesos_pb2.TASK_FAILED, 'load failed: %s' % e)

        # killed, lost, load failed
        job.statusUpdate(task_id, tried, state, status.data)
        #if state in (mesos_pb2.TASK_FAILED, mesos_pb2.TASK_LOST):
        #    self.slaveFailed[slave_id] = self.slaveFailed.get(slave_id,0) + 1
    
    def jobFinished(self, job):
        logger.debug("job %s finished", job.id)
        if job.id in self.activeJobs:
            del self.activeJobs[job.id]
            self.activeJobsQueue.remove(job) 
            for id in self.jobTasks[job.id]:
                del self.taskIdToJobId[id]
                del self.taskIdToSlaveId[id]
            del self.jobTasks[job.id]
            self.last_finish_time = time.time()

            if not self.activeJobs:
                self.slaveTasks.clear()
                self.slaveFailed.clear()

    @safe
    def check(self):
        for job in self.activeJobs.values():
            if job.check_task_timeout():
                self.requestMoreResources()

    @safe
    def error(self, driver, code, message):
        logger.warning("Mesos error message: %s (code: %s)", message, code)
        #if self.activeJobs:
        #    self.requestMoreResources()

    #@safe
    def stop(self):
        if not self.started:
            return
        logger.debug("stop scheduler")
        self.started = False
        self.isRegistered = False
        self.driver.stop(False)
        self.driver = None

    def defaultParallelism(self):
        return 16

    def frameworkMessage(self, driver, slave, executor, data):
        logger.warning("[slave %s] %s", slave.value, data)

    def disconnected(self, driver):
        logger.warning("mesos master disconnected")

    def reregistered(self, driver, masterInfo):
        logger.warning("re-connect to mesos master %s:%s(%s)", 
            int2ip(masterInfo.ip), masterInfo.port, masterInfo.id)

    def executorLost(self, driver, executorId, slaveId, status):
        logger.warning("executor at %s %s lost: %s", slaveId.value, executorId.value, status)
        del self.slaveTasks[slaveId.value]

    def slaveLost(self, driver, slave):
        logger.warning("slave %s lost", slave.value)
        self.slaveTasks.pop(slave.value, 0)
        self.slaveFailed[slave.value] = MAX_FAILED

    def killTask(self, job_id, task_id, tried):
        tid = mesos_pb2.TaskID()
        tid.value = "%s:%s:%s" % (job_id, task_id, tried)
        self.driver.killTask(tid)
