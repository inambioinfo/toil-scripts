#!/usr/bin/env bash

# Create s3am venv
rm -rf s3am
virtualenv --never-download s3am
s3am/bin/pip install s3am==2.0a1.dev105
# Expose binaries to the PATH
mkdir bin
ln -snf ${PWD}/s3am/bin/s3am bin/
export PATH=$PATH:${PWD}/bin

# Create Toil venv
rm -rf venv
virtualenv --never-download venv
. venv/bin/activate
# Adding AWS extra to get boto as required by tests
pip install toil[aws]==3.5.0a1.dev277

# Prepare directory for temp files
TMPDIR=/mnt/ephemeral/tmp
rm -rf $TMPDIR
mkdir $TMPDIR
export TMPDIR

make develop
make test
make clean

# clean working copy to satisfy corresponding check in Makefile
rm -rf bin s3am
make pypi

rm -rf venv $TMPDIR
