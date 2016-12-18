from multiprocessing import Queue, Event
from glycan_profiling.serialize.hypothesis.peptide import Peptide, Protein

from .proteomics.peptide_permutation import ProteinDigestor
from .proteomics.fasta import ProteinFastaFileParser
from .common import (
    GlycopeptideHypothesisSerializerBase, DatabaseBoundOperation,
    PeptideGlycosylator, PeptideGlycosylatingProcess)


class FastaGlycopeptideHypothesisSerializer(GlycopeptideHypothesisSerializerBase):
    def __init__(self, fasta_file, connection, glycan_hypothesis_id, hypothesis_name=None,
                 protease='trypsin', constant_modifications=None, variable_modifications=None,
                 max_missed_cleavages=2, max_glycosylation_events=1):
        GlycopeptideHypothesisSerializerBase.__init__(self, connection, hypothesis_name, glycan_hypothesis_id)
        self.fasta_file = fasta_file
        self.protease = protease
        self.constant_modifications = constant_modifications
        self.variable_modifications = variable_modifications
        self.max_missed_cleavages = max_missed_cleavages
        self.max_glycosylation_events = max_glycosylation_events
        self.set_parameters({
            "fasta_file": fasta_file,
            "enzymes": [protease],
            "constant_modifications": constant_modifications,
            "variable_modifications": variable_modifications,
            "max_missed_cleavages": max_missed_cleavages,
            "max_glycosylation_events": max_glycosylation_events
        })

    def extract_proteins(self):
        i = 0
        for protein in ProteinFastaFileParser(self.fasta_file):
            protein.hypothesis_id = self.hypothesis_id
            self.session.add(protein)
            i += 1
            if i % 10000 == 0:
                self.log("%d Proteins Extracted" % (i,))
                self.session.commit()

        self.session.commit()

    def protein_ids(self):
        return [i[0] for i in self.query(Protein.id).filter(Protein.hypothesis_id == self.hypothesis_id).all()]

    def peptide_ids(self):
        return [i[0] for i in self.query(Peptide.id).filter(Peptide.hypothesis_id == self.hypothesis_id).all()]

    def digest_proteins(self):
        digestor = ProteinDigestor(
            self.protease, self.constant_modifications, self.variable_modifications,
            self.max_missed_cleavages)
        i = 0
        j = 0
        protein_ids = self.protein_ids()
        n = len(protein_ids)
        interval = min(n / 10., 100000)
        acc = []
        for protein_id in protein_ids:
            i += 1
            protein = self.query(Protein).get(protein_id)
            if i % interval == 0:
                self.log("%0.3f%% Complete (%d/%d). %d Peptides Produced." % (i * 100. / n, i, n, j))
            for peptide in digestor.process_protein(protein):
                acc.append(peptide)
                j += 1
                if len(acc) > 100000:
                    self.session.bulk_save_objects(acc)
                    self.session.commit()
                    acc = []
        self.session.bulk_save_objects(acc)
        self.session.commit()
        acc = []

    def glycosylate_peptides(self):
        glycosylator = PeptideGlycosylator(self.session, self.hypothesis_id)
        acc = []
        i = 0
        for peptide_id in self.peptide_ids():
            peptide = self.query(Peptide).get(peptide_id)
            for glycopeptide in glycosylator.handle_peptide(peptide):
                acc.append(glycopeptide)
                i += 1
                if len(acc) > 100000:
                    self.session.bulk_save_objects(acc)
                    self.session.commit()
                    acc = []
        self.session.bulk_save_objects(acc)
        self.session.commit()

    def run(self):
        self.log("Extracting Proteins")
        self.extract_proteins()
        self.log("Digesting Proteins")
        self.digest_proteins()
        self.log("Combinating Glycans")
        self.combinate_glycans(self.max_glycosylation_events)
        self.log("Building Glycopeptides")
        self.glycosylate_peptides()
        self._sql_analyze_database()
        self._count_produced_glycopeptides()
        self.log("Done")


class MultipleProcessFastaGlycopeptideHypothesisSerializer(FastaGlycopeptideHypothesisSerializer):
    def __init__(self, fasta_file, connection, glycan_hypothesis_id, hypothesis_name=None,
                 protease='trypsin', constant_modifications=None, variable_modifications=None,
                 max_missed_cleavages=2, max_glycosylation_events=1, n_processes=4):
        super(MultipleProcessFastaGlycopeptideHypothesisSerializer, self).__init__(
            fasta_file, connection, glycan_hypothesis_id, hypothesis_name,
            protease, constant_modifications, variable_modifications,
            max_missed_cleavages, max_glycosylation_events)
        self.n_processes = n_processes

    def glycosylate_peptides(self):
        input_queue = Queue(10)
        done_event = Event()
        processes = [
            PeptideGlycosylatingProcess(
                self._original_connection, self.hypothesis_id, input_queue,
                chunk_size=2000, done_event=done_event) for i in range(self.n_processes)
        ]
        peptide_ids = self.peptide_ids()
        n = len(peptide_ids)
        i = 0
        chunk_size = min(int(n * 0.05), 1000)
        for process in processes:
            input_queue.put(peptide_ids[i:(i + chunk_size)])
            i += chunk_size
            process.start()

        while i < n:
            input_queue.put(peptide_ids[i:(i + chunk_size)])
            i += chunk_size
            self.log("... Dealt Peptides %d-%d %0.2f%%" % (i - chunk_size, min(i, n), (min(i, n) / float(n)) * 100))

        self.log("... All Peptides Dealt")
        done_event.set()
        for process in processes:
            process.join()
