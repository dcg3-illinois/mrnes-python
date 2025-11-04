#!/bin/bash
# This script runs unit tests for all Python files in the evt/tests directory
# and also builds and runs Go comparison files if they exist.

cd tests/
for dir in */ ; do
    # run unit tests on the python files in each directory
    ut_file="${dir%/}/test_${dir%/}.py"
    if [ -f "$ut_file" ]; then
        pytest -v "$ut_file"
    fi

    # get the name of the go file, build it, and run the python test file
    go_file="${dir%/}/go_${dir%/}_compare.go"
    if [ -f "$go_file" ]; then
        go build -o "${dir%/}/go_${dir%/}_compare" "$go_file"
    fi
    test_file="${dir%/}/test_conversion_${dir%/}.py"
    echo ${test_file}
    if [ -f "$test_file" ]; then
        pytest -v "$test_file"
    fi
done
