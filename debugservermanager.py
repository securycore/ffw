#!/usr/bin/python

import logging
import time
import signal

from ptrace.debugger.debugger import PtraceDebugger
from ptrace.debugger.child import createChild
from ptrace.debugger.process_event import ProcessExit
from ptrace.debugger.ptrace_signal import ProcessSignal

import serverutils
import networkmanager
import socket

# This is a Queue that behaves like stdout
class StdoutQueue():
    def __init__(self,*args,**kwargs):
        self.q = args[0]

    def write(self,msg):
        self.q.put(msg)

    def flush(self):
        pass


class DebugServerManager(object):

    def __init__(self, config, queue_sync, queue_out, targetPort):
        self.config = config
        self.queue_sync = queue_sync
        self.queue_out = queue_out
        self.targetPort = targetPort

        self.pid = None
        self.dbg = None
        self.crashEvent = None
        self.proc = None

        stdoutQueue = StdoutQueue(queue_out)
        serverutils.setupEnvironment(config)


    # entry function for process
    # should be the only public function
    def startAndWait(self):
        # Sadly this does not apply to child processes started via
        # createChild(), so we can only capture output of this python process
        #sys.stdout = stdoutQueue
        #sys.stderr = stdoutQueue

        self.queue_out.put("Dummy")
        # do not remove print, parent excepts something
        logging.info("DebugServer: Start Server")
        #sys.stderr.write("Stderr")

        self._startServer()

        # notify parent about the pid
        self.queue_sync.put( ("pid", self.pid) )

        if self._waitForCrash():
            crashData = self._getCrashDetails(self.crashEvent)
        else:
            crashData = None

        logging.debug("DebugServer: send to queue_sync")
        self.queue_sync.put( ("data", crashData) )

        self.dbg.quit()


    def _startServer(self):
        # create child via ptrace debugger
        # API: createChild(arguments[], no_stdout, env=None)
        self.pid = createChild(
            serverutils.getInvokeTargetArgs(self.config, self.targetPort),
            True, # no_stdout
            None,
        )

        # Attach to the process with ptrace and let it run
        self.dbg = PtraceDebugger()
        self.proc = self.dbg.addProcess(self.pid, True)
        self.proc.cont()


    def _stopServer(self):
        try:
            os.kill(self.pid, signal.SIGTERM)
        except:
            # is already dead...
            pass


    def _waitForCrash(self):
        while True:
            logging.info("DebugServer: Waiting for process event")
            event = self.dbg.waitProcessEvent()
            logging.info("DebugServer: Got event: " + str(event))
            # If this is a process exit we need to check if it was abnormal
            if type(event) == ProcessExit:
                if event.signum is None or event.exitcode == 0:
                    # Clear the event since this was a normal exit
                    event = None

            # If this is a signal we need to check if we're ignoring it
            elif type(event) == ProcessSignal:
                if event.signum == signal.SIGCHLD:
                    # Ignore these signals and continue waiting
                    continue
                elif event.signum == signal.SIGTERM:
                    # server cannot be started, return
                    event = None
                    queue_sync.put( ("err", event.signum) )

            break

        if event is not None and event.signum != 15:
            logging.info("DebugServer: Event Result: Crash")
            self.crashEvent = event
            return True
        else:
            logging.info("DebugServer: Event Result: No crash")
            self.crashEvent = None
            return False


    def _getCrashDetails(self, event):
        # Get the address where the crash occurred
        faultAddress = event.process.getInstrPointer()

        # Find the module that contains this address
        # Now we need to turn the address into an offset. This way when the process
        # is loaded again if the module is loaded at another address, due to ASLR,
        # the offset will be the same and we can correctly detect those as the same
        # crash
        module = None
        faultOffset = 0
        try:
            for mapping in event.process.readMappings():
                if faultAddress >= mapping.start and faultAddress < mapping.end:
                    module = mapping.pathname
                    faultOffset = faultAddress - mapping.start
                    break
        except Exception as error:
            #print "getCrashDetails Exception: " + str(error)
            # it always has a an exception...
            pass

        # Apparently the address didn't fall within a mapping
        if module is None:
            module = "Unknown"
            faultOffset = faultAddress

        # Get the signal
        sig = event.signum

        # Get the details of the crash
        details = None
        if event._analyze() is not None:
            details = event._analyze().text

        crashData = {
            "faultOffset": faultOffset,
            "module": module,
            "sig": sig,
            "details": details,
        }
        crashData["asanOutput"] = serverutils.getAsanOutput(self.config, self.pid)

        return crashData