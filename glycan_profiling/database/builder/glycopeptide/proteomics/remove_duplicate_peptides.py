from glycan_profiling.serialize.utils import temp_table
from glycan_profiling.serialize import (
    Peptide, Protein, DatabaseBoundOperation,
    TemplateNumberStore)

from glycan_profiling.task import log_handle


class DeduplicatePeptides(DatabaseBoundOperation):
    def __init__(self, connection, hypothesis_id):
        DatabaseBoundOperation.__init__(self, connection)
        self.hypothesis_id = hypothesis_id

    def run(self):
        remove_duplicates(self.session, self.hypothesis_id)


def find_best_peptides(session, hypothesis_id):
    q = session.query(
        Peptide.id, Peptide.peptide_score,
        Peptide.modified_peptide_sequence, Peptide.protein_id, Peptide.start_position).join(
        Protein).filter(Protein.hypothesis_id == hypothesis_id).yield_per(10000)
    keepers = dict()
    for id, score, modified_peptide_sequence, protein_id, start_position in q:
        try:
            old_id, old_score = keepers[modified_peptide_sequence, protein_id, start_position]
            if score > old_score:
                keepers[modified_peptide_sequence, protein_id, start_position] = id, score
        except KeyError:
            keepers[modified_peptide_sequence, protein_id, start_position] = id, score
    return keepers


def store_best_peptides(session, keepers):
    table = temp_table(TemplateNumberStore)
    conn = session.connection()
    table.create(conn)
    payload = [{"value": x[0]} for x in keepers.values()]
    conn.execute(table.insert(), payload)
    session.commit()
    return table


def remove_duplicates(session, hypothesis_id):
    log_handle.log("... Extracting Best Peptides")
    keepers = find_best_peptides(session, hypothesis_id)
    table = store_best_peptides(session, keepers)
    ids = session.query(table.c.value)
    log_handle.log("... Selecting ID Mask")
    q = session.query(Peptide.id).filter(
        Peptide.protein_id == Protein.id,
        Protein.hypothesis_id == hypothesis_id,
        ~Peptide.id.in_(ids.correlate(None)))
    log_handle.log("... Deleting Duplicates")
    session.execute(Peptide.__table__.delete(
        Peptide.__table__.c.id.in_(q.selectable)))
    conn = session.connection()
    table.drop(conn)
    log_handle.log("... Complete")
    session.commit()
