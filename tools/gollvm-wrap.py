#!/usr/bin/python
"""Wrapper to selectively run gollvm instead of gccgo.

This is a shim script that intercepts invocations of 'gccgo' and then
in turn invokes either the real gccgo driver or a copy of gollvm
instead, depending on the arguments and on environment variables.

When performing a Go build with gccgo, the Go command will typically
invoke gccgo once for each compilation step, which might look like

  gccgo -I ... -o objfile.o -g <options> file.go file2.go ... fileN.go

and then a final invocation will be made at the link step, e.g.

  gccgo -L ... somearchive.a ... -o binary

The goal of this shim is to convert invocations of the first form to
llvm-goparse invocations, and to ignore invocations of the second form
and just pass them on to gccgo.

We also tack on a set of additional "-L" options to the llvm-goparse
invocation so that it can find the go runtime libraries, and intercept
the "-o" option so that we can run the asembler afterwards.

To use this script, you will need a copy of GCCGO, e.g. the directory
produced by running "make all && make install" in a GCCGO build tree.
From within the gccgo install dir, run

   gollvm-wrap.py --install

This will modify the install directory to insert the wrapper into the
compilation path.

"""

import getopt
import os
import re
import subprocess
import sys

import script_utils as u

# Echo command before executing
flag_echo = True

# Dry run mode
flag_dryrun = False

# gccgo only mode
flag_nollvm = False

# trace llvm-goparse invocations
flag_trace_llinvoc = False


def docmd(cmd):
  """Execute a command."""
  if flag_echo:
    sys.stderr.write("executing: " + cmd + "\n")
  if flag_dryrun:
    return
  u.docmd(cmd)


def form_golibargs(driver):
  """Form correct go library args."""
  ddir = os.path.dirname(driver)
  bdir = os.path.dirname(ddir)
  cmd = "find %s/lib64 -name runtime.gox -print" % bdir
  lines = u.docmdlines(cmd)
  if not lines:
    u.error("no output from %s -- bad gccgo install dir?" % cmd)
  line = lines[0]
  rdir = os.path.dirname(line)
  u.verbose(1, "libdir is %s" % rdir)
  return ["-L", rdir]


def perform():
  """Main driver routine."""
  global flag_trace_llinvoc

  u.verbose(1, "argv: %s" % " ".join(sys.argv))

  # llvm-goparse should be available somewhere in PATH, error if not
  lines = u.docmdlines("which llvm-goparse", True)
  if not lines:
    u.error("no 'llvm-goparse' in PATH -- can't proceed")

  # Perform a walk of the command line arguments looking for Go files.
  reg = re.compile(r"^\S+\.go$")
  foundgo = False
  for clarg in sys.argv[1:]:
    m = reg.match(clarg)
    if m:
      foundgo = True
      break

  if not foundgo or flag_nollvm:
    # No go files. Invoke real gccgo.
    bd = os.path.dirname(sys.argv[0])
    driver = "%s/gccgo.real" % bd
    u.verbose(1, "driver path is %s" % driver)
    args = [sys.argv[0]] + sys.argv[1:]
    u.verbose(1, "args: '%s'" % " ".join(args))
    if not os.path.exists(driver):
      usage("internal error: %s does not exist [most likely this "
            "script was not installed correctly]" % driver)
    os.execv(driver, args)
    u.error("exec failed: %s" % driver)

  # These hold the arguments of -I and -L options
  largs = []
  iargs = []

  # Create a set of massaged args.
  nargs = []
  skipc = 0
  outfile = None
  asmfile = None
  minus_s = False
  for ii in range(1, len(sys.argv)):
    clarg = sys.argv[ii]
    if skipc != 0:
      skipc -= 1
      continue
    if clarg == "-S":
      minus_s = True
      continue
    if clarg == "-o":
      outfile = sys.argv[ii+1]
      skipc = 1
      continue
    if clarg == "-I":
      skipc = 1
      iarg = sys.argv[ii+1]
      iargs.append(iarg)
      continue
    if clarg == "-L":
      skipc = 1
      larg = sys.argv[ii+1]
      largs.append(larg)
      continue
    if clarg == "-v":
      flag_trace_llinvoc = True
    nargs.append(clarg)

  if not outfile:
    u.error("fatal error: unable to find -o "
            "option in clargs: %s" % " ".join(sys.argv))

  if minus_s:
    asmfile = "%s" % outfile
  else:
    asmfile = "%s.s" % outfile
  nargs.append("-o")
  nargs.append(asmfile)

  golibargs = form_golibargs(sys.argv[0])
  nargs += golibargs
  if largs:
    nargs.append("-L")
    nargs.append(":".join(largs))
  if iargs:
    nargs.append("-I")
    nargs.append(":".join(iargs))
  u.verbose(1, "revised args: %s" % " ".join(nargs))

  # Invoke gollvm.
  driver = "llvm-goparse"
  u.verbose(1, "driver path is %s" % driver)
  nargs = ["llvm-goparse"] + nargs
  if flag_trace_llinvoc:
    u.verbose(0, "+ %s" % " ".join(nargs))
  rc = subprocess.call(nargs)
  if rc != 0:
    u.verbose(1, "return code %d from %s" % (rc, " ".join(nargs)))
    return 1

  # Invoke the assembler
  if not minus_s:
    ascmd = "as %s -o %s" % (asmfile, outfile)
    u.verbose(1, "asm command is: %s" % ascmd)
    rc = u.docmdnf(ascmd)
    if rc != 0:
      u.verbose(1, "return code %d from %s" % (rc, ascmd))
      return 1

  return 0


def install_shim(scriptpath):
  """Install shim into gccgo install dir."""

  # Make sure we're in the right place (gccgo install dir)
  if not os.path.exists("bin"):
    usage("expected to find bin subdir")
  if not os.path.exists("lib64/libgo.so"):
    usage("expected to find lib64/libgo.so")
  if not os.path.exists("bin/gccgo"):
    usage("expected to find bin/gccgo")

  # Copy script, or update if already in place.
  docmd("cp %s bin" % scriptpath)
  sdir = os.path.dirname(scriptpath)
  docmd("cp %s/script_utils.py bin" % sdir)

  # Test to see if script installed already
  cmd = "file bin/gccgo"
  lines = u.docmdlines(cmd)
  if not lines:
    u.error("no output from %s -- bad gccgo install dir?" % cmd)
  else:
    reg = re.compile(r"^.+ ELF .+$")
    m = reg.match(lines[0])
    if not m:
      u.warning("wrapper appears to be installed already in this dir")
      return

  # Move aside the real gccgo binary
  docmd("mv bin/gccgo bin/gccgo.real")

  # Emit a script into gccgo
  sys.stderr.write("emitting wrapper script into bin/gccgo\n")
  if not flag_dryrun:
    try:
      with open("./bin/gccgo", "w") as wf:
        here = os.getcwd()
        wf.write("#!/bin/sh\n")
        wf.write("P=%s/bin/gollvm-wrap.py\n" % here)
        wf.write("exec python ${P} $*\n")
    except IOError:
      u.error("open/write failed for bin/gccgo wrapper")
  docmd("chmod 0755 bin/gccgo")

  # Success
  u.verbose(0, "wrapper installed successfully")

  # Done
  return 0


def usage(msgarg):
  """Print usage and exit."""
  if msgarg:
    sys.stderr.write("error: %s\n" % msgarg)
  print """\
    usage:  %s <gccgo args>

    Options (via command line)
    --install   installs wrapper into gccgo directory

    Options (via GOLLVM_WRAP_OPTIONS):
    -t          trace llvm-goparse executions
    -d          increase debug msg verbosity level
    -e          show commands being invoked
    -D          dry run (echo cmds but do not execute)
    -G          pure gccgo compile (no llvm-goparse invocations)

    """ % os.path.basename(sys.argv[0])
  sys.exit(1)


def parse_env_options():
  """Option parsing from env var."""
  global flag_echo, flag_dryrun, flag_nollvm, flag_trace_llinvoc

  optstr = os.getenv("GOLLVM_WRAP_OPTIONS")
  if not optstr:
    return
  args = optstr.split()

  try:
    optlist, _ = getopt.getopt(args, "detDG")
  except getopt.GetoptError as err:
    # unrecognized option
    usage(str(err))

  for opt, _ in optlist:
    if opt == "-d":
      u.increment_verbosity()
    elif opt == "-e":
      flag_echo = True
    elif opt == "-t":
      flag_trace_llinvoc = True
    elif opt == "-D":
      flag_dryrun = True
    elif opt == "-G":
      flag_nollvm = True
  u.verbose(1, "env var options parsing complete")


# Setup
u.setdeflanglocale()
parse_env_options()

# Either --install mode or regular mode
if len(sys.argv) == 2 and sys.argv[1] == "--install":
  prc = install_shim(sys.argv[0])
else:
  prc = perform()
sys.exit(prc)
