"""
Hook into Windows post-mortem debugger facility

https://docs.microsoft.com/en-us/windows-hardware/drivers/debugger/enabling-postmortem-debugging
"""

import sys
import os
import time
import logging
import errno
import traceback
import subprocess as SP
from glob import glob

from . import CommonDumper, _root_dir

AeDebug = r'Software\Microsoft\Windows NT\CurrentVersion\AeDebug'
AeDebug6432 = r'Software\Wow6432Node\Microsoft\Windows NT\CurrentVersion\AeDebug'

_log = logging.getLogger(__name__)

def syncfd(F):
    lck = F.name+'.lck'
    while os.path.isfile(lck):
        time.sleep(1.0)

def reg_replace(bits, kname, vname, value):
    try:
        import winreg
    except ImportError:
        import _winreg as winreg

    access = winreg.KEY_READ|winreg.KEY_WRITE
    access |= {
        32:winreg.KEY_WOW64_32KEY,
        64:winreg.KEY_WOW64_64KEY,
    }[bits]

    key = winreg.OpenKeyEx(winreg.HKEY_LOCAL_MACHINE, kname, 0, access)

    try:
        prev, type = winreg.QueryValueEx(key, vname)
        assert type==winreg.REG_SZ, type
    except WindowsError as e:
        if e.errno!=errno.ENOENT:
            raise
        prev = None

    winreg.SetValueEx(key, vname, 0, winreg.REG_SZ, value)
    winreg.FlushKey(key)

    return prev

def cdbsearch():
    ret = []
    for cdb in glob(r'C:\Program Files (x86)\Windows Kits\*\Debuggers\{}\cdb.exe'.format(os.environ['PLATFORM'].lower())):
        ret.append(os.path.dirname(cdb))
    return ret

def binsearch():
    '''Find all directories containing .exe .dll and .lib
    '''
    ret = set()
    for root, subdirs, files in os.walk(os.getcwd()):
        for file in files:
            if os.path.splitext(file)[1] in ('.exe', '.dll', '.lib'):
                ret.add(root)
    return list(ret)

class WindowsDumper(CommonDumper):
    def install(self):
        os.environ['PATH'] = os.pathsep.join([os.environ['PATH']] + cdbsearch())

        cdb = self.findbin(self.args.debugger or 'cdb.exe')
        cmd = self.findbin(self.args.debugger or 'cmd.exe')

        sympath = binsearch()
        [_log.debug('Sympath %s', spath) for spath in sympath]

        self.mkdirs(self.args.outdir)

        dumper = os.path.join(self.args.outdir, 'dumper.bat')
        with open(dumper, 'w') as F:
            F.write(r'''
cd "{args.outdir}"
set "PYTHONPATH={cwd}"
set "_NT_SYMBOL_PATH={sympath}"
"{sys.executable}" -m ci_core_dumper.windows --outdir "{args.outdir}" --cdb "{cdb}" --pid %1 --event %2
'''.format(sys=sys,
           cwd=_root_dir,
           cdb=cdb,
           args=self.args,
           sympath='*'.join(sympath)))

        debugger = '"{}" /c "{}" %ld %ld'.format(cmd, dumper)

        reg_replace(32, AeDebug, 'Debugger', debugger)
        reg_replace(32, AeDebug, 'Auto', '1')
        # on 64
        reg_replace(64, AeDebug, 'Debugger', debugger)
        reg_replace(64, AeDebug, 'Auto', '1')
        reg_replace(64, AeDebug6432, 'Debugger', debugger)
        reg_replace(64, AeDebug6432, 'Auto', '1')

    def uninstall(self):
        _log.warning('uninstall not implemented')

    def report(self):
        for log in glob(os.path.join(self.args.outdir, '*.txt')):
            self.catfile(log, sync=syncfd)

        self.catfile(os.path.join(self.args.outdir, 'core-dumper.log'))

    def crash(self):
        if self.args.direct:
            from ctypes.util import find_msvcrt
            from ctypes import CDLL
            CDLL(find_msvcrt() or 'msvcrt').abort()

        else:
            CommonDumper.crash(self)

def getargs():
    from argparse import ArgumentParser
    P = ArgumentParser()
    P.add_argument('--cdb')
    P.add_argument('--pid')
    P.add_argument('--event')
    P.add_argument('--outdir')
    return P

def dump():
    dtime = time.time()
    args = getargs().parse_args()

    logging.basicConfig(level=logging.DEBUG, filename=os.path.join(args.outdir, 'core-dumper.log'))

    _log.debug('Dumping PID %s @ %s', args.pid, dtime)
    try:
        os.chdir(args.outdir)

        cdbfile = '{}.{}.cdb'.format(dtime, args.pid)
        logfile  = '{}.{}.txt'.format(dtime, args.pid)
        lckfile = logfile+'.lck'

        with open(lckfile, 'w') as LCK, open(logfile, 'w') as LOG:
            LOG.write('PID: {}\n'.format(args.pid))
            try:

                with open(cdbfile, 'w') as F:
                    F.write('''
.logopen "{log}"
.symfix+ c:\symcache
.sympath
.echo Modules list
lm;
.echo Stacks
~* kP n
.echo analysis
!analyze
.echo End
q
'''.format(log=logfile))

                cmd = [
                    args.cdb,
                    '-p', args.pid,
                    '-e', args.event,
                    #'-netsyms', 'no',
                    '-noio',
                    '-lines',
                    '-g', '-G',
                    '-cf', cdbfile,
                    #'-c', 'lm;~* kv n;q;',
                ]

                _log.debug('exec: %s', cmd)
                LOG.flush()

                proc = SP.Popen(cmd, stdout=SP.PIPE, stderr=SP.STDOUT,
                                close_fds=False, creationflags=SP.CREATE_NEW_PROCESS_GROUP)
                timeout = {}
                if sys.version_info>=(3,3):
                    try:
                        trace, _unused = proc.communicate(timeout=20.0)
                    except SP.TimeoutExpired:
                        LOG.flush()
                        LOG.seek(0,2)
                        LOG.write('cdb TIMEOUT\n')
                        proc.kill()
                        trace, _unused = proc.communicate()
                else:
                    trace, _unused = proc.communicate()

                LOG.flush()
                LOG.seek(0,2)

                code = proc.poll()
                if code:
                    LOG.write('ERROR: {}\n'.format(code))

                LOG.write(trace.decode('ascii'))
                LOG.write('\nComplete\n')

            except:
                traceback.print_exc(file=LOG)
            finally:
                # always flush before unlock
                LOG.flush()

    except:
        _log.exception('oops')
    finally:
        os.remove(lckfile)

if __name__=='__main__':
    dump()
