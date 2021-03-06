#!/usr/bin/env python
"""

    %prog [options] train-file [test-file]


NOTE: this expects svm-train and svm-predict to be on your path. so you may call with:

    PATH=/dir/containing/libsvm:$PATH %prog ...
"""


import sys
import os
import os.path as op
import numpy as np
import random
from subprocess import Popen, PIPE
from multiprocessing import cpu_count

import optparse

def check_path():
    """
    make sure the libsvm stuff is on the path
    """
    paths = (op.abspath(p) for p in os.environ['PATH'].split(":"))
    for p in paths:
        if op.exists(op.join(p, "svm-train")): return True
    for path in ("../", "./"):
        if op.exists(path + "svm-train"):
            os.environ['PATH'] += ":" + path
            return True

    print >>sys.stderr, "\n** svm-train not found in path **\n" + ("*" * 80)

    return False

def up_to_date_b(a, b):
    return op.exists(b) and os.stat(b).st_mtime >= os.stat(a).st_mtime

def scale(train_dataset, test_dataset, out_prefix):
    cmd_tmpl = 'svm-train -c %(c)f -g %(g)f -v %(fold)i %(extra_params)s %(train_dataset)s'

    range_file = out_prefix + ".range"
    scaled_train = train_dataset + ".scale"
    scaled_test = test_dataset + ".scale" if test_dataset else None

    # only rescale if the input dataset has changed.
    if not (up_to_date_b(train_dataset, range_file) \
            and up_to_date_b(train_dataset, scaled_train)):
        print >>sys.stderr, "Scaling: %s" % train_dataset
        cmd = 'svm-scale -s "%(range_file)s" "%(train_dataset)s" > "%(scaled_train)s"' % locals()
        p = Popen(cmd, shell=True, stdout=PIPE)
        p.wait()
        assert p.returncode == 0, (p.stdout.read())

    if not test_dataset:
        return scaled_train, test_dataset

    # scale the test file according to range in train file.
    if not (up_to_date_b(test_dataset, range_file) \
            and up_to_date_b(test_dataset, scaled_test)):
        print >>sys.stderr, "Scaling: %s" % test_dataset
        cmd = 'svm-scale -r "%(range_file)s" "%(test_dataset)s" > "%(scaled_test)s"' % locals()
        p = Popen(cmd, shell=True, stdout=PIPE)
        p.wait()
        assert p.returncode == 0, (p.stdout.read())

    return scaled_train, scaled_test

def do_split(full_dataset, split_pct):
    #n_lines = sum(1 for line in open(full_dataset) if line[0] != "#")
    train_fh = open(full_dataset + ".train.split", "w")
    test_fh = open(full_dataset + ".test.split", "w")

    for line in open(full_dataset):
        if line[0] == "#":
            print >>train_fh, line,
            print >>test_fh, line,
            continue
        r = random.random()
        fh = test_fh if r > split_pct else train_fh
        print >>fh, line,

    train_fh.close(); test_fh.close()
    names = train_fh.name, test_fh.name
    print >>sys.stderr, "split to: %s, %s" % names
    return names

def roc(actual, predicted, out_file):
    """
    code taken from scikits.learn.metrics: 
        http://scikit-learn.sourceforge.net/index.html(thanks).
    """
    actual = np.array(actual)
    predicted = np.array(predicted)

    actual[actual != 1] = 0

    thresholds = np.sort(np.unique(predicted))[::-1]
    n_thresholds = thresholds.size

    tpr = np.empty(n_thresholds) # True positive rate
    fpr = np.empty(n_thresholds) # False positive rate
    n_pos = float(np.sum(actual == 1)) # nb of true positive
    n_neg = float(np.sum(actual == 0)) # nb of true negative

    for i, t in enumerate(thresholds):
        tpr[i] = np.sum(actual[predicted >= t] == 1) / n_pos
        fpr[i] = np.sum(actual[predicted >= t] == 0) / n_neg

    h = np.diff(fpr)
    auc = np.sum(h * (tpr[1:] + tpr[:-1])) / 2.0

    print >>open(out_file, "w"), "\n".join(("%.4f,%.4f" % (f, t) for (f, t) in zip(fpr, tpr)))
    return auc


def main():
    kernels = ["linear", "polynomial", "rbf", "sigmoid"]
    p = optparse.OptionParser(__doc__)
    p.add_option("--kernel", dest="kernel", default="rbf",
            help="one of %s" % "/".join(kernels))
    p.add_option("--c-range", dest="c_range", default="-7:5:2",
            help="log2 range of values in format start:stop:step [%default]")
    p.add_option("--g-range", dest="g_range", default="-16:4:2",
            help="log2 range of g values in format start:stop:step [%default]")
    p.add_option("--n-threads", dest="n_threads", default=cpu_count(), type='int',
            help="number of threads to use [%default]")
    p.add_option("--out-prefix", dest="out_prefix",
            help="where to send results")
    p.add_option("--x-fold", dest="x_fold", type="int", default=8,
            help="number for cross-fold validation on training set [%default]")
    p.add_option("--scale", dest="scale", action="store_true", default=False,
            help="if specified, perform scaling (svm-scale) on the dataset(s)"
                " before calling svm-train. [%default]")
    p.add_option("--split", dest="split", type='float',
            help="if specified split the training file into 2 files. one for"
            " testing and one for training. --split 0.8 would use 80% of the"
            " lines for training. the selection is random. this is used "
            "instead of specifying a training file.")
    p.add_option("-b", "--probability", dest="b", action="store_true", default=False,
            help="calculate and store prediction as a probability rather "
            " than a class. [%default]")

    opts, args = p.parse_args()
    if len(args) < 1 or not check_path(): sys.exit(p.print_help())
    if not opts.kernel in kernels:
        print >>sys.stderr, "** kernel must be one of %s" % ",".join(kernels)
        sys.exit(p.print_help())

    # convert to the number expected by libsvm
    kernel = kernels.index(opts.kernel) + 1

    train_dataset = op.abspath(args[0])
    assert op.exists(train_dataset)

    test_dataset = op.abspath(args[1]) if len(args) > 1 else None
    if test_dataset: assert op.exists(test_dataset)

    c_range = map(float, opts.c_range.split(":"))
    g_range = map(float, opts.g_range.split(":"))

    out_prefix = opts.out_prefix if opts.out_prefix else op.splitext(train_dataset)[0]
    # set parameters
    param_list = gen_params(c_range, g_range)

    if opts.scale:
        print >>sys.stderr, "Scaling datasets"
        train_dataset, test_dataset = scale(train_dataset, test_dataset, out_prefix)
    if opts.split:
        assert test_dataset is None, ("cant split *and* specify a test dataset")
        train_dataset, test_dataset = do_split(train_dataset, opts.split)

    param_fh = open(out_prefix + ".params", "w")

    b, fold = int(opts.b), opts.x_fold
    extra_params = ""
    results = {}
    print >>sys.stderr, "Training across %i gridded parameter groups in batches of %i" \
                    % (len(param_list), opts.n_threads)
    cmd_tmpl = 'svm-train -b %(b)i -t %(kernel)i -m 1000 -c %(c)f -g %(g)f -v %(fold)i %(extra_params)s %(train_dataset)s'

    while param_list:
        procs = []
        print len(param_list), "remaining\r",
        sys.stdout.flush()
        for i in range(opts.n_threads):
            if not param_list: break
            c, g = param_list.pop()
            run_cmd = cmd_tmpl % locals()
            procs.append((run_cmd, c, g, Popen(run_cmd, shell=True, stdout=PIPE)))
        for cmd, c, g, p in procs:
            p.wait()
            for line in p.stdout:
                if not "Cross" in line: continue
                validation = float(line.split()[-1][0:-1])
                if results and validation > max(results.keys()): line = line.strip() + " *BEST*\n"
                results[validation] = (c, g)
                print >>param_fh, "-c %s, -g %s # accuracy: %s" % (c, g, validation)

    # grab the best one.
    valid_pct, (c, g) = sorted(results.items())[-1]
    print "Best Cross Validation Accuracy: %.2f with parameters c:%s,  g:%s" %\
            (valid_pct, c, g)
    print "wrote all params and accuracies to:", param_fh.name
    if test_dataset is None: return True

    cmd_tmpl = 'svm-train -b %(b)i -t %(kernel)i -c %(c)f -g %(g)f %(extra_params)s %(train_dataset)s %(model_file)s'
    model_file = out_prefix + ".model"
    print "Saving model file to %s" % model_file

    # run once more and save the .model file for the best scoring params.
    Popen(cmd_tmpl % locals(), shell=True, stdout=PIPE).wait()

    # now run the test dataset through svm-predict with best parameters
    predict_file = out_prefix + ".predict"
    # TODO: add -b param
    cmd_tmpl = "svm-predict -b %(b)i %(test_dataset)s %(model_file)s %(predict_file)s"
    p = Popen(cmd_tmpl % locals(), shell=True, stdout=PIPE)
    print p.stdout.read().strip()

    # can only do roc if b is 1.
    if b == 1:
        predicted = [float(line.split(" ")[1]) for line in open(predict_file) \
                                if not line.startswith('labels')]

        actual = [int(line.split()[0]) for line in open(test_dataset)]
        print "AUC:", roc(actual, predicted, out_prefix + ".roc.txt")


def gen_params(c_range, g_range):
    return [(2**c, 2**g) for c in np.arange(*c_range) for g in np.arange(*g_range)]

if __name__ == "__main__":
    main()
