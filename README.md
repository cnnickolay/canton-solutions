# Canton Solutions

Reference implementations for the Canton Network, written in Daml.

Each solution lives in its own directory with its packages, tests and a short design note explaining the choices and trade-offs.

## Solutions

1. [Sharded batched vault](vault/README.md). A CIP-0112 vault that settles deposits and redemptions in batches across parallel shards, with the totals and the price on ledger. It scales horizontally: 8 shards settled 3.9 times as many orders a second as a single book on a three-node Canton network.

## License

[PolyForm Noncommercial 1.0.0](LICENSE). You may use, copy and change this code for any non-commercial purpose, such as personal projects, study or research. Any commercial use needs my written permission first, ask me through [GitHub](https://github.com/cnnickolay).

The vendored Splice DARs in `vault/dars` keep their own Apache-2.0 licence.
