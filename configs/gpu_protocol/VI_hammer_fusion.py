# Copyright (c) 2006-2007 The Regents of The University of Michigan
# Copyright (c) 2009 Advanced Micro Devices, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met: redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer;
# redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution;
# neither the name of the copyright holders nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# Authors: Brad Beckmann

import math
import m5
import VI_hammer
from m5.objects import *
from m5.defines import buildEnv
from Cluster import Cluster

#
# Note: the L1 Cache latency is only used by the sequencer on fast path hits
#
class L1Cache(RubyCache):
    latency = 1

#
# Note: the L2 Cache latency is not currently used
#
class L2Cache(RubyCache):
    latency = 15

def create_system(options, system, piobus, dma_devices, ruby_system):

    if not buildEnv['GPGPU_SIM']:
        m5.util.panic("This script requires GPGPU-Sim integration to be built.")

    # Run the protocol script to setup CPU cluster, directory and DMA
    (all_sequencers, dir_cntrls, dma_cntrls, cpu_cluster) = \
                                        VI_hammer.create_system(options,
                                                                system,
                                                                piobus,
                                                                dma_devices,
                                                                ruby_system)

    cpu_cntrl_count = len(cpu_cluster) + len(dir_cntrls)

    #
    # Build GPU cluster
    #
    gpu_cluster = Cluster(intBW = 32, extBW = 32)
    gpu_cluster.disableConnectToParent()

    l2_bits = int(math.log(options.num_l2caches, 2))
    block_size_bits = int(math.log(options.cacheline_size, 2))
    # This represents the L1 to L2 interconnect latency
    # NOTE! This latency is in Ruby (cache) cycles, not SM cycles
    per_hop_interconnect_latency = 45 # ~15 GPU cycles
    num_dance_hall_hops = int(math.log(options.num_sc, 2))
    if num_dance_hall_hops == 0:
        num_dance_hall_hops = 1
    l1_to_l2_noc_latency = per_hop_interconnect_latency * num_dance_hall_hops

    #
    # Caches for GPU cores
    #
    for i in xrange(options.num_sc):
        #
        # First create the Ruby objects associated with the GPU cores
        #
        cache = L1Cache(size = options.sc_l1_size,
                            assoc = options.sc_l1_assoc,
                            replacement_policy = "LRU",
                            start_index_bit = block_size_bits,
                            dataArrayBanks = 4,
                            tagArrayBanks = 4,
                            dataAccessLatency = 4,
                            tagAccessLatency = 4,
                            resourceStalls = False)

        l1_cntrl = GPUL1Cache_Controller(version = i,
                                  cntrl_id = cpu_cntrl_count + len(gpu_cluster),
                                  cache = cache,
                                  l2_select_num_bits = l2_bits,
                                  num_l2 = options.num_l2caches,
                                  issue_latency = l1_to_l2_noc_latency,
                                  number_of_TBEs = options.gpu_l1_buf_depth,
                                  ruby_system = ruby_system)

        gpu_seq = RubySequencer(version = options.num_cpus + i,
                            icache = cache,
                            dcache = cache,
                            access_phys_mem = True,
                            max_outstanding_requests = options.gpu_l1_buf_depth,
                            ruby_system = ruby_system,
                            deadlock_threshold = 2000000)

        l1_cntrl.sequencer = gpu_seq

        if piobus != None:
            gpu_seq.pio_port = piobus.slave

        exec("ruby_system.l1_cntrl_sp%02d = l1_cntrl" % i)

        #
        # Add controllers and sequencers to the appropriate lists
        #
        all_sequencers.append(gpu_seq)
        gpu_cluster.add(l1_cntrl)

    l2_index_start = block_size_bits + l2_bits
    # Use L2 cache and interconnect latencies to calculate protocol latencies
    # NOTE! These latencies are in Ruby (cache) cycles, not SM cycles
    l2_cache_access_latency = 30 # ~10 GPU cycles
    l2_to_l1_noc_latency = per_hop_interconnect_latency * num_dance_hall_hops
    l2_to_mem_noc_latency = 125 # ~40 GPU cycles

    l2_clusters = []
    for i in xrange(options.num_l2caches):
        #
        # First create the Ruby objects associated with this cpu
        #
        l2_cache = L2Cache(size = options.sc_l2_size,
                           assoc = options.sc_l2_assoc,
                           start_index_bit = l2_index_start,
                           replacement_policy = "LRU",
                           dataArrayBanks = 4,
                           tagArrayBanks = 4,
                           dataAccessLatency = 4,
                           tagAccessLatency = 4,
                           resourceStalls = options.gpu_l2_resource_stalls)

        l2_cntrl = GPUL2Cache_Controller(version = i,
                                cntrl_id = cpu_cntrl_count + len(gpu_cluster),
                                L2cache = l2_cache,
                                l2_response_latency = l2_cache_access_latency +
                                                      l2_to_l1_noc_latency,
                                l2_request_latency = l2_to_mem_noc_latency,
                                ruby_system = ruby_system)

        exec("ruby_system.l2_cntrl%d = l2_cntrl" % i)
        l2_cluster = Cluster(intBW = 32, extBW = 32)
        l2_cluster.add(l2_cntrl)
        gpu_cluster.add(l2_cluster)
        l2_clusters.append(l2_cluster)

    ############################################################################
    # Pagewalk cache
    # NOTE: We use a CPU L1 cache controller here. This is to facilatate MMU
    #       cache coherence (as the GPU L1 caches are incoherent without flushes
    #       The L2 cache is small, and should have minimal affect on the 
    #       performance (see Section 6.2 of Power et al. HPCA 2014).
    pwd_cache = L1Cache(size = options.pwc_size,
                            assoc = 16, # 64 is fully associative @ 8kB
                            replacement_policy = "LRU",
                            start_index_bit = block_size_bits,
                            latency = 8,
                            resourceStalls = False)
    # Small cache since CPU L1 requires I and D
    pwi_cache = L1Cache(size = "512B",
                            assoc = 2,
                            replacement_policy = "LRU",
                            start_index_bit = block_size_bits,
                            latency = 8,
                            resourceStalls = False)

    # Small cache since CPU L1 controller requires L2
    l2_cache = L2Cache(size = "512B",
                           assoc = 2,
                           start_index_bit = block_size_bits,
                           latency = 1,
                           resourceStalls = False)

    l1_cntrl = L1Cache_Controller(version = options.num_cpus,
                                  cntrl_id = len(cpu_cluster)+len(gpu_cluster)+
                                             len(dir_cntrls),
                                  L1Icache = pwi_cache,
                                  L1Dcache = pwd_cache,
                                  L2cache = l2_cache,
                                  send_evictions = False,
                                  issue_latency = l1_to_l2_noc_latency,
                                  cache_response_latency = 1,
                                  l2_cache_hit_latency = 1,
                                  number_of_TBEs = options.gpu_l1_buf_depth,
                                  ruby_system = ruby_system)

    cpu_seq = RubySequencer(version = options.num_cpus + options.num_sc,
                            icache = pwd_cache, # Never get data from pwi_cache
                            dcache = pwd_cache,
                            access_phys_mem = True,
                            max_outstanding_requests = options.gpu_l1_buf_depth,
                            ruby_system = ruby_system,
                            deadlock_threshold = 2000000)

    l1_cntrl.sequencer = cpu_seq


    ruby_system.l1_pw_cntrl = l1_cntrl
    all_sequencers.append(cpu_seq)

    gpu_cluster.add(l1_cntrl)


    #
    # Create controller for the copy engine to connect to in GPU cluster
    # Cache is unused by controller
    #
    cache = L1Cache(size = "4096B", assoc = 2)

    gpu_ce_seq = RubySequencer(version = options.num_cpus + options.num_sc+1,
                               icache = cache,
                               dcache = cache,
                               access_phys_mem = True,
                               max_outstanding_requests = 64,
                               support_inst_reqs = False,
                               ruby_system = ruby_system)

    gpu_ce_cntrl = GPUCopyDMA_Controller(version = 0,
                                  cntrl_id = cpu_cntrl_count + len(gpu_cluster),
                                  sequencer = gpu_ce_seq,
                                  number_of_TBEs = 256,
                                  ruby_system = ruby_system)

    ruby_system.l1_cntrl_ce = gpu_ce_cntrl

    all_sequencers.append(gpu_ce_seq)

    complete_cluster = Cluster(intBW = 32, extBW = 32)
    complete_cluster.add(gpu_ce_cntrl)
    complete_cluster.add(cpu_cluster)
    complete_cluster.add(gpu_cluster)

    for cntrl in dir_cntrls:
        complete_cluster.add(cntrl)

    for cntrl in dma_cntrls:
        cntrl.cntrl_id = len(complete_cluster)
        complete_cluster.add(cntrl)

    for cluster in l2_clusters:
        complete_cluster.add(cluster)

    return (all_sequencers, dir_cntrls, complete_cluster)
