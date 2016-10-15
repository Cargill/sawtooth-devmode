# Copyright 2016 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------------------------------------------------------

import collections
import logging
import importlib

from time import time

from gossip import common, stats
from journal import journal_core
from journal.consensus.poet0 import poet_transaction_block
from journal.consensus.poet0.wait_timer import WaitTimer
from journal.consensus.poet0.wait_certificate import WaitCertificate

logger = logging.getLogger(__name__)


class PoetJournal(journal_core.Journal):
    """Implements a journal based on the proof of elapsed time
    consensus mechanism.

    Attributes:
        onHeartBeatTimer (EventHandler): The EventHandler tracking
            calls to make when the heartbeat timer fires.
        MaximumBlocksToKeep (int): The maximum number of blocks to
            keep.
    """

    def __init__(self,
                 local_node,
                 gossip,
                 gossip_dispatcher,
                 stat_domains,
                 kwargs,
                 minimum_transactions_per_block=None,
                 max_transactions_per_block=None,
                 max_txn_age=None,
                 genesis_ledger=None,
                 data_directory=None,
                 store_type=None):
        """Constructor for the PoetJournal class.

        Args:
            node (Node): The local node.
        """
        super(PoetJournal, self).__init__(
            local_node,
            gossip,
            gossip_dispatcher,
            stat_domains,
            minimum_transactions_per_block,
            max_transactions_per_block,
            max_txn_age,
            genesis_ledger,
            data_directory,
            store_type)

        if 'PoetEnclaveImplementation' in kwargs:
            enclave_module = kwargs['PoetEnclaveImplementation']
        else:
            enclave_module = 'journal.consensus.poet0.poet_enclave_simulator' \
                             '.poet0_enclave_simulator'

        poet_enclave = importlib.import_module(enclave_module)
        poet_enclave.initialize(**kwargs)
        WaitCertificate.poet_enclave = poet_enclave
        WaitTimer.poet_enclave = poet_enclave

        # initialize the poet handlers
        poet_transaction_block.register_message_handlers(self)

        # initialize stats specifically for the block chain journal
        self.JournalStats.add_metric(stats.Value('LocalMeanTime', 0))
        self.JournalStats.add_metric(stats.Value('AggregateLocalMean', '0'))
        self.JournalStats.add_metric(stats.Value('PopulationEstimate', '0'))
        self.JournalStats.add_metric(stats.Value('ExpectedExpirationTime',
                                                 '0'))
        self.JournalStats.add_metric(stats.Value('Duration', '0'))

        # propagate the maximum blocks to keep
        self.maximum_blocks_to_keep = max(self.maximum_blocks_to_keep,
                                          WaitTimer.certificate_sample_length)

        self.dispatcher.on_heartbeat += self._check_certificate

    def build_transaction_block(self, genesis=False):
        """Builds a transaction block that is specific to this particular
        consensus mechanism, in this case we build a block that contains a
        wait certificate.

        Args:
            genesis (boolean): Whether to force creation of the initial
                block.

        Returns:
            PoetTransactionBlock: The constructed block with the wait
                certificate.
        """
        logger.debug('attempt to build transaction block extending %s',
                     self.most_recent_committed_block_id[:8])
        with self._txn_lock:
            # Create a new block from all of our pending transactions
            nblock = poet_transaction_block.PoetTransactionBlock()
            nblock.BlockNum = self.most_recent_committed_block.BlockNum \
                + 1 if self.most_recent_committed_block else 0
            nblock.PreviousBlockID = self.most_recent_committed_block_id

            self.on_pre_build_block.fire(self, nblock)

            # Get the list of prepared transactions, if there aren't enough
            # then just return
            txnlist = self._prepare_transaction_list(
                self.maximum_transactions_per_block)
            transaction_time_waiting = time() - self.transaction_enqueue_time\
                if self.transaction_enqueue_time is not None else 0
            if len(txnlist) < self.minimum_transactions_per_block and\
                    not genesis and\
                    transaction_time_waiting <\
                    self.MaximumTransactionsWaitTime:
                logger.debug('Not enough transactions(%d, %d required) to '
                             'build block, no block constructed. Mandatory'
                             'block creation in %f seconds',
                             len(txnlist),
                             self.minimum_transactions_per_block,
                             self.MaximumTransactionsWaitTime -
                             transaction_time_waiting)
                return None
            else:
                # we know that the transaction list is a subset of the
                # pending transactions, if it is less then all of them
                # then set the TransactionEnqueueTime we can track these
                # transactions wait time.
                remaining_transactions = len(self.pending_transactions) - \
                    len(txnlist)
                self.transaction_enqueue_time =\
                    time() if remaining_transactions > 0 else None

            logger.info('build transaction block to extend %s with %s '
                        'transactions',
                        self.most_recent_committed_block_id[:8], len(txnlist))

            # Create a new block from all of our pending transactions
            nblock = poet_transaction_block.PoetTransactionBlock()
            nblock.BlockNum = self.most_recent_committed_block.BlockNum \
                + 1 if self.most_recent_committed_block else 0
            nblock.PreviousBlockID = self.most_recent_committed_block_id
            nblock.TransactionIDs = txnlist

            nblock.create_wait_timer(
                self.local_node.signing_address(),
                self._build_certificate_list(nblock))

            self.JournalStats.LocalMeanTime.Value = nblock.WaitTimer.local_mean
            self.JournalStats.PopulationEstimate.Value = \
                round(nblock.WaitTimer.local_mean /
                      nblock.WaitTimer.target_wait_time, 2)

            if genesis:
                nblock.AggregateLocalMean = nblock.WaitTimer.local_mean

            self.JournalStats.PreviousBlockID.Value = nblock.PreviousBlockID

            # must put a cap on the transactions in the block
            if len(nblock.TransactionIDs) >= \
                    self.maximum_transactions_per_block:
                nblock.TransactionIDs = \
                    nblock.TransactionIDs[:self.maximum_transactions_per_block]

            logger.debug('created new pending block with timer <%s> and '
                         '%d transactions', nblock.WaitTimer,
                         len(nblock.TransactionIDs))

            self.JournalStats.ExpectedExpirationTime.Value = \
                round(nblock.WaitTimer.request_time +
                      nblock.WaitTimer.duration, 2)

            self.JournalStats.Duration.Value = \
                round(nblock.WaitTimer.duration, 2)

            for txnid in nblock.TransactionIDs:
                txn = self.transaction_store[txnid]
                txn.InBlock = "Uncommitted"
                self.transaction_store[txnid] = txn
            # fire the build block event handlers
            self.on_build_block.fire(self, nblock)

            return nblock

    def claim_transaction_block(self, block):
        """Claims the block and transmits a message to the network
        that the local node won.

        Args:
            block (PoetTransactionBlock): The block to claim.
        """
        logger.info('node %s validates block with %d transactions',
                    self.local_node.Name, len(block.TransactionIDs))

        # Claim the block
        block.create_wait_certificate()
        block.sign_from_node(self.local_node)
        self.JournalStats.BlocksClaimed.increment()

        # Fire the event handler for block claim
        self.on_claim_block.fire(self, block)

        # And send out the message that we won
        msg = poet_transaction_block.PoetTransactionBlockMessage()
        msg.TransactionBlock = block
        self.gossip.broadcast_message(msg)

        self.pending_transaction_block = None

    def _build_certificate_list(self, block):
        # for the moment we just dump all of these into one list,
        # not very efficient but it makes things a lot easier to maintain
        certs = collections.deque()
        count = WaitTimer.certificate_sample_length

        while block.PreviousBlockID != common.NullIdentifier \
                and len(certs) < count:
            block = self.block_store[block.PreviousBlockID]
            certs.appendleft(block.WaitCertificate)

        # drop the root block off the computation
        return list(certs)

    def _check_certificate(self, now):
        with self._txn_lock:
            if self.pending_transaction_block:
                if self.pending_transaction_block.wait_timer_is_expired(now):
                    self.claim_transaction_block(
                        self.pending_transaction_block)
            else:
                # No transaction block - check if we must make one due to time
                # waited
                if self.transaction_enqueue_time is not None:
                    transaction_time_waiting = \
                        time() - self.transaction_enqueue_time
                else:
                    transaction_time_waiting = 0
                if transaction_time_waiting > self.MaximumTransactionsWaitTime:
                    logger.debug("Transaction wait timeout "
                                 "calling build block")
                    self.pending_transaction_block = \
                        self.build_transaction_block()
