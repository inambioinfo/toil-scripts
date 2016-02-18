#!/usr/bin/env python2.7
"""
@author Frank Austin Nothaft fnothaft@berkeley.edu
@date 12/30/2015

Pipeline to go from FASTQ to VCF using both the ADAM+HaplotypeCaller pipeline
as well as the GATK best practices pipeline.

  0 --> ... --> 4 --> 5
                |     |++(6)
                |     7 --> 9 --> ... --> 12 --> 13 --> ... --> 17
                |     ++(8)                                     |
                |                                               18
                |                                              /  \
                |                                            19    20
                |                                           /        \
                |                                         21          22
                |
                |
                + --> 23 --> ... --> 34 --> 35 --> ... --> 39
                                                           |
                                                           40
                                                          /  \
                                                        41    42
                                                       /        \
                                                      43         44


BWA alignment

0   bwa alignment to a reference
1   samtools sam to bam conversion (no sort)
2   Fix header
3   Add read groups
4   Upload to S3

ADAM preprocessing

5   Start master
6   Master Service
7   Start Workers
8   Worker Service
9   Download Data
10  ADAM Convert
11  ADAM Transform
12  Upload Data

GATK haplotype caller

13  Start GATK box
14  Download reference
15  Index reference
16  Build reference dictionary
17  Index samples
18  Run HaplotypeCaller
19  Run VQSR on SNPs
20  Run VQSR on INDELs
21  Apply VQSR model to SNPs
22  Apply VQSR model to INDELs

GATK preprocessing

23  Download shared data
24  Reference preprocessing
25  Download sample 
26  Index
27  Sort
28  Mark duplicates
29  Index
30  Realigner target 
31  Indel realignment
32  Index
33  Base recalibration
34  Output BQSR file

GATK haplotype caller

35  Start GATK box
36  Download reference
37  Index reference
38  Build reference dictionary
39  Index samples
40  Run HaplotypeCaller
41  Run VQSR on SNPs
42  Run VQSR on INDELs
43  Apply VQSR model to SNPs
44  Apply VQSR model to INDELs


However, the pipeline in this file is actually just five encapsulated jobs:

        A
       / \
      B   D
      |   |
      C   E

A  Run BWA alignment (jobs 0-4)
B  Run ADAM preprocessing (jobs 5-12)
C  Run GATK haplotype caller (jobs 13-22)
D  Run GATK preprocessing (jobs 23-34)
E  Run GATK haplotype caller (jobs 35-44)

===================================================================
:Dependencies:
curl            - apt-get install curl
Toil            - pip install --pre toil
Docker          - http://docs.docker.com/engine/installation/

Optional:
S3AM            - pip install --s3am (requires ~/.boto config file)
"""

# import from python system libraries
import argparse
import multiprocessing
import os

# import toil features
from toil.job import Job

# import job steps from other toil pipelines
from toil_scripts.adam_pipeline.spark_toil_script import *
from toil_scripts.batch_alignment.bwa_alignment import *
from toil_scripts.gatk_germline.germline import *
from toil_scripts.gatk_processing.gatk_preprocessing import *

def build_parser():

    parser = argparse.ArgumentParser()

    # add sample uuid
    parser.add_argument('-U', '--uuid_manifest', required = True,
                        help = 'Sample UUID.')

    # what pipeline are we running
    parser.add_argument('-PR', '--pipeline_to_run',
                        help = "Whether we should run 'adam', 'gatk', or 'both'. Default is 'both'.",
                        default = 'both')

    # add bucket args
    parser.add_argument('-3', '--s3_bucket', required = True,
                        help = 'S3 Bucket URI')
    parser.add_argument('-3r', '--bucket_region', default = "us-west-2",
                        help = 'Region of the S3 bucket. Defaults to us-east-1.')

    # add file size argument
    parser.add_argument('-se', '--file_size', default = '100G',
                        help = 'Approximate input file size. Should be given as %d[TGMK], e.g., for a 100 gigabyte file, use --file_size 100G')

    # add bwa args
    parser.add_argument('-r', '--ref', required = True,
                        help = 'Reference fasta file')
    parser.add_argument('-m', '--amb', required = True,
                        help = 'Reference fasta file (amb)')
    parser.add_argument('-n', '--ann', required = True,
                        help = 'Reference fasta file (ann)')
    parser.add_argument('-b', '--bwt', required = True,
                        help = 'Reference fasta file (bwt)')
    parser.add_argument('-p', '--pac', required = True,
                        help = 'Reference fasta file (pac)')
    parser.add_argument('-a', '--sa', required = True,
                        help = 'Reference fasta file (sa)')
    parser.add_argument('-f', '--fai', required = True,
                        help = 'Reference fasta file (fai)')
    parser.add_argument('-u', '--sudo', dest = 'sudo', action = 'store_true',
                        help = 'Docker usually needs sudo to execute '
                        'locally, but not''when running Mesos '
                        'or when a member of a Docker group.')
    parser.add_argument('-k', '--use_bwakit', action='store_true', help='Use bwakit instead of the binary build of bwa')
    parser.add_argument('-t', '--alt', required=False, help='Alternate file for reference build (alt). Necessary for alt aware alignment.')
    parser.set_defaults(sudo = False)

    # add ADAM args
    parser.add_argument('-N', '--num_nodes', type = int, required = True,
                        help = 'Number of nodes to use')
    parser.add_argument('-d', '--driver_memory', required = True,
                        help = 'Amount of memory to allocate for Spark Driver.')
    parser.add_argument('-q', '--executor_memory', required = True,
                        help = 'Amount of memory to allocate per Spark Executor.')

    # add GATK args
    parser.add_argument('-P', '--phase', required = True,
                        help = '1000G_phase1.indels.b37.vcf URL')
    parser.add_argument('-M', '--mills', required = True,
                        help = 'Mills_and_1000G_gold_standard.indels.b37.vcf URL')
    parser.add_argument('-s', '--dbsnp', required = True,
                        help = 'dbsnp_137.b37.vcf URL')
    parser.add_argument('-O', '--omni', required = True,
                        help = '1000G_omni.5.b37.vcf URL')
    parser.add_argument('-H', '--hapmap', required = True,
                        help = 'hapmap_3.3.b37.vcf URL')
    
    # return built parser
    return parser


def sample_loop(job, bucket_region, s3_bucket, uuid_list, bwa_inputs, adam_inputs, gatk_preprocess_inputs, gatk_adam_call_inputs, gatk_gatk_call_inputs, pipeline_to_run):
  """
  Loops over the sample_ids (uuids) in the manifest, creating child jobs to process each
  """

  for uuid in uuid_list:

    ## set uuid inputs
    bwa_inputs['lb'] = uuid
    bwa_inputs['uuid'] = uuid
    adam_inputs['outDir'] = "s3://%s/analysis/%s" % (s3_bucket, uuid)
    adam_inputs['bamName'] = "s3://%s/alignment/%s.bam" % (s3_bucket, uuid)
    gatk_preprocess_inputs['s3_dir'] =  "%s/analysis/%s" % (s3_bucket, uuid)
    gatk_adam_call_inputs['s3_dir'] = "%s/analysis/%s" % (s3_bucket, uuid)
    gatk_gatk_call_inputs['s3_dir'] = "%s/analysis/%s" % (s3_bucket, uuid)

    job.addChildJobFn(static_dag, bucket_region, s3_bucket, uuid, bwa_inputs, adam_inputs, gatk_preprocess_inputs, gatk_adam_call_inputs, gatk_gatk_call_inputs, pipeline_to_run )
    
  

def static_dag(job, bucket_region, s3_bucket, uuid, bwa_inputs, adam_inputs, gatk_preprocess_inputs, gatk_adam_call_inputs, gatk_gatk_call_inputs, pipeline_to_run):
    """
    Prefer this here as it allows us to pull the job functions from other jobs
    without rewrapping the job functions back together.

    bwa_inputs: Input arguments to be passed to BWA.
    adam_inputs: Input arguments to be passed to ADAM.
    gatk_preprocess_inputs: Input arguments to be passed to GATK preprocessing.
    gatk_adam_call_inputs: Input arguments to be passed to GATK haplotype caller for the result of ADAM preprocessing.
    gatk_gatk_call_inputs: Input arguments to be passed to GATK haplotype caller for the result of GATK preprocessing.
    """

    # get work directory
    work_dir = job.fileStore.getLocalTempDir()

    # what region is our bucket in?
    if bucket_region == "us-east-1":
        bucket_region = ""
    else:
        bucket_region = "-%s" % bucket_region

    # does the work directory exist?
    if not os.path.exists(work_dir):
        os.mkdirs(work_dir)

    # write config for bwa
    bwa_config_path = os.path.join(work_dir, "%s_bwa_config.csv" % uuid)
    bwafp = open(bwa_config_path, "w")
    print >> bwafp, "%s,https://s3%s.amazonaws.com/%s/sequence/%s_1.fastq.gz,https://s3%s.amazonaws.com/%s/sequence/%s_2.fastq.gz" % (uuid, bucket_region, s3_bucket, uuid, bucket_region, s3_bucket, uuid)
    bwafp.flush()
    bwafp.close()
    bwa_inputs['config'] = job.fileStore.writeGlobalFile(bwa_config_path)

    # write config for GATK preprocessing
    gatk_preprocess_config_path = os.path.join(work_dir, "%s_gatk_preprocess_config.csv" % uuid)
    gatk_preprocess_fp = open(gatk_preprocess_config_path, "w")
    print >> gatk_preprocess_fp, "%s,https://s3%s.amazonaws.com/%s/alignment/%s.bam" % (uuid, bucket_region, s3_bucket, uuid)
    gatk_preprocess_fp.flush()
    gatk_preprocess_fp.close()
    gatk_preprocess_inputs['config'] = job.fileStore.writeGlobalFile(gatk_preprocess_config_path)

    # write config for GATK haplotype caller for the result of ADAM preprocessing
    gatk_adam_call_config_path = os.path.join(work_dir, "%s_gatk_adam_call_config.csv" % uuid)
    gatk_adam_call_fp = open(gatk_adam_call_config_path, "w")
    print >> gatk_adam_call_fp, "%s,https://s3%s.amazonaws.com/%s/analysis/%s/%s.adam.bam" % (uuid, bucket_region, s3_bucket, uuid, uuid)
    gatk_adam_call_fp.flush()
    gatk_adam_call_fp.close()
    gatk_adam_call_inputs['config'] = job.fileStore.writeGlobalFile(gatk_adam_call_config_path)

    # write config for GATK haplotype caller for the result of GATK preprocessing
    gatk_gatk_call_config_path = os.path.join(work_dir, "%s_gatk_gatk_call_config.csv" % uuid)
    gatk_gatk_call_fp = open(gatk_gatk_call_config_path, "w")
    print >> gatk_gatk_call_fp, "%s,https://s3%s.amazonaws.com/%s/analysis/%s/%s.gatk.bam" % (uuid, bucket_region, s3_bucket, uuid, uuid)
    gatk_gatk_call_fp.flush()
    gatk_gatk_call_fp.close()
    gatk_gatk_call_inputs['config'] = job.fileStore.writeGlobalFile(gatk_gatk_call_config_path)

    # get head BWA alignment job function and encapsulate it
    bwa = job.wrapJobFn(download_shared_files,
                        bwa_inputs).encapsulate()

    # get head ADAM preprocessing job function and encapsulate it
    adam_preprocess = job.wrapJobFn(start_master,
                                    adam_inputs).encapsulate()

    # get head GATK preprocessing job function and encapsulate it
    gatk_preprocess = job.wrapJobFn(download_gatk_files,
                                    gatk_preprocess_inputs).encapsulate()

    # get head GATK haplotype caller job function for the result of ADAM preprocessing and encapsulate it
    gatk_adam_call = job.wrapJobFn(batch_start,
                                   gatk_adam_call_inputs).encapsulate()

    # get head GATK haplotype caller job function for the result of GATK preprocessing and encapsulate it
    gatk_gatk_call = job.wrapJobFn(batch_start,
                                   gatk_gatk_call_inputs).encapsulate()

    

    # wire up dag
    job.addChild(bwa)
   
    if (pipeline_to_run == "adam" or
        pipeline_to_run == "both"):
        bwa.addChild(adam_preprocess)
        adam_preprocess.addChild(gatk_adam_call)

    if (pipeline_to_run == "gatk" or
        pipeline_to_run == "both"):
        bwa.addChild(gatk_preprocess)
        gatk_preprocess.addChild(gatk_gatk_call)
   

if __name__ == '__main__':
    
    args_parser = build_parser()
    Job.Runner.addToilOptions(args_parser)
    args = args_parser.parse_args()

    ## Parse manifest file
    uuid_list = []
    with open(args.uuid_manifest) as f_manifest:
      for uuid in f_manifest:
        uuid_list.append(uuid.strip())

    
    bwa_inputs = {'ref.fa': args.ref,
                  'ref.fa.amb': args.amb,
                  'ref.fa.ann': args.ann,
                  'ref.fa.bwt': args.bwt,
                  'ref.fa.pac': args.pac,
                  'ref.fa.sa': args.sa,
                  'ref.fa.fai': args.fai,
                  'ref.fa.alt': args.alt,
                  'ssec': None,
                  'output_dir': None,
                  'sudo': args.sudo,
                  's3_dir': "%s/alignment" % args.s3_bucket,
                  'cpu_count': None,
                  'file_size': args.file_size,
                  'use_bwakit': args.use_bwakit}
    
    if args.num_nodes <= 1:
        raise ValueError("--num_nodes allocates one Spark/HDFS master and n-1 workers, and thus must be greater than 1. %d was passed." % args.num_nodes)

    adam_inputs = {'numWorkers': args.num_nodes - 1,
                   'knownSNPs':  args.dbsnp.replace("https://s3-us-west-2.amazonaws.com/", "s3://"),
                   'driverMemory': args.driver_memory,
                   'executorMemory': args.executor_memory,
                   'sudo': args.sudo,
                   'suffix': '.adam'}

    gatk_preprocess_inputs = {'ref.fa': args.ref,
                              'phase.vcf': args.phase,
                              'mills.vcf': args.mills,
                              'dbsnp.vcf': args.dbsnp,
                              'output_dir': None,
                              'sudo': args.sudo,
                              'ssec': None,
                              'cpu_count': str(multiprocessing.cpu_count()),
                              'suffix': '.gatk' }
    
    gatk_adam_call_inputs = {'ref.fa': args.ref,
                             'phase.vcf': args.phase,
                             'mills.vcf': args.mills,
                             'dbsnp.vcf': args.dbsnp,
                             'hapmap.vcf': args.hapmap,
                             'omni.vcf': args.omni,
                             'output_dir': None,
                             'uuid': None,
                             'cpu_count': str(multiprocessing.cpu_count()),
                             'ssec': None,
                             'file_size': args.file_size,
                             'suffix': '.adam',
                             'sudo': args.sudo}

    gatk_gatk_call_inputs = {'ref.fa': args.ref,
                             'phase.vcf': args.phase,
                             'mills.vcf': args.mills,
                             'dbsnp.vcf': args.dbsnp,
                             'hapmap.vcf': args.hapmap,
                             'omni.vcf': args.omni,
                             'output_dir': None,
                             'uuid': None,
                             'cpu_count': str(multiprocessing.cpu_count()),
                             'ssec': None,
                             'file_size': args.file_size,
                             'suffix': '.gatk',
                             'sudo': args.sudo}

    if (args.pipeline_to_run != "adam" and
        args.pipeline_to_run != "gatk" and
        args.pipeline_to_run != "both"):
        raise ValueError("--pipeline_to_run must be either 'adam', 'gatk', or 'both'. %s was passed." % args.pipeline_to_run)

    Job.Runner.startToil(Job.wrapJobFn(sample_loop,
                                       args.bucket_region,
                                       args.s3_bucket,
                                       uuid_list,
                                       bwa_inputs,
                                       adam_inputs,
                                       gatk_preprocess_inputs,
                                       gatk_adam_call_inputs,
                                       gatk_gatk_call_inputs,
                                       args.pipeline_to_run), args)