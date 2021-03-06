"""
A module for generating sequence signatures for the given two sets of proteins.
"""
from django.conf import settings
from django.core import exceptions

from alignment.functions import strip_html_tags, get_format_props
Alignment = getattr(__import__(
    'common.alignment_' + settings.SITE_NAME,
    fromlist=['Alignment']
    ), 'Alignment')

from common.definitions import AMINO_ACIDS, AMINO_ACID_GROUPS, AMINO_ACID_GROUP_NAMES
from protein.models import Protein, ProteinConformation
from residue.models import Residue


from collections import OrderedDict
from copy import deepcopy
import numpy as np
import re
import time

class SequenceSignature:
    """
    A class handling the sequence signature.
    """

    def __init__(self):

        self.aln_pos = Alignment()
        self.aln_neg = Alignment()

        self.features_normalized_pos = OrderedDict()
        self.features_normalized_neg = OrderedDict()
        self.features_frequency_difference = OrderedDict()
        self.features_frequency_diff_display = []

        self.freq_cutoff = 30
        self.common_gn = OrderedDict()

        self.feature_preference = prepare_aa_group_preference()
        self.group_lengths = dict([
            (x, len(y)) for x,y in enumerate(AMINO_ACID_GROUPS.values())
        ])
        self.default_column = np.array([((y.startswith('-') or y == '_') and y != '--' and not y.startswith('-_') and 100) or 0 for y in AMINO_ACID_GROUPS.keys()])


    def _assign_preferred_features(self, signature, segment, ref_matrix):

        new_signature = []
        for pos, argmax in enumerate(signature):
            new_signature.append(self._calculate_best_feature(pos, segment, argmax, ref_matrix))
        return new_signature


    def _calculate_best_feature(self, pos, segment, argmax, ref_matrix):

        tmp = self.feature_preference[argmax]
        equiv_feat = np.where(np.isin(ref_matrix[segment][:, pos], ref_matrix[segment][argmax][pos]))[0]
        pref_feat = argmax
        min_len = self.group_lengths[argmax]

        for efeat in equiv_feat:
            if efeat in tmp and self.group_lengths[efeat] < min_len:
                pref_feat = efeat
                min_len = self.group_lengths[efeat]
        return pref_feat

    def setup_alignments(self, segments, protein_set_positive = None, protein_set_negative = None):

        if protein_set_positive:
            self.aln_pos.load_proteins(protein_set_positive)
        if protein_set_negative:
            self.aln_neg.load_proteins(protein_set_negative)

        # In case positive and negative sets come from different classes
        # unify the numbering schemes
        self.common_schemes = self.merge_numbering_schemes()
        self.aln_pos.numbering_schemes = self.common_schemes
        self.aln_neg.numbering_schemes = self.common_schemes
        # now load the segments and generic numbers
        self.aln_pos.load_segments(segments)
        self.aln_neg.load_segments(segments)

        self.aln_pos.build_alignment()
        self.aln_neg.build_alignment()

        self.common_gn = deepcopy(self.aln_pos.generic_numbers)
        for scheme in self.aln_neg.numbering_schemes:
            for segment in self.aln_neg.segments:
                for pos in self.aln_neg.generic_numbers[scheme[0]][segment].items():
                    if pos[0] not in self.common_gn[scheme[0]][segment].keys():
                        self.common_gn[scheme[0]][segment][pos[0]] = pos[1]
                self.common_gn[scheme[0]][segment] = OrderedDict(sorted(
                    self.common_gn[scheme[0]][segment].items(),
                    key=lambda x: x[0].split('x')
                    ))
        self.common_segments = OrderedDict([
            (x, sorted(list(set(self.aln_pos.segments[x]) | set(self.aln_neg.segments[x])), key=lambda x: x.split('x'))) for x in self.aln_neg.segments
        ])
        # tweaking alignment
        self.aln_pos.calculate_statistics()
        self._update_alignment(self.aln_pos)
        # tweaking consensus seq
        self._update_consensus_sequence(self.aln_pos)

        # tweaking negative alignment
        self.aln_neg.calculate_statistics()
        self._update_alignment(self.aln_neg)
        # tweaking consensus seq
        self._update_consensus_sequence(self.aln_neg)

    def _update_alignment(self, alignment):

        for prot in alignment.proteins:
            for seg, resi in prot.alignment.items():
                consensus = []
                aln_list = [x[0] for x in resi]
                aln_dict = dict([
                    (x[0], x) for x in resi
                ])
                for pos in self.common_segments[seg]:
                    if pos not in aln_list:
                        consensus.append([pos, False, '_', 0])
                    else:
                        consensus.append(aln_dict[pos])
                prot.alignment[seg] = consensus

    def _update_consensus_sequence(self, alignment):

        for seg, resi in alignment.consensus.items():
            consensus = OrderedDict()
            aln_list = [x for x in resi.keys()]
            aln_dict = dict([
                (x, resi[x]) for x in resi.keys()
            ])
            for pos in self.common_segments[seg]:
                if pos not in aln_list:
                    consensus[pos] = ['-', 0, 100]
                else:
                    consensus[pos] = aln_dict[pos]
            alignment.consensus[seg] = consensus

    def _convert_feature_stats(self, fstats, aln):

        tmp_fstats = []
        for row in range(len(AMINO_ACID_GROUPS.keys())):
            tmp_row = []
            for segment in self.common_segments:
                tmp_row.append([[
                    str(x),
                    str(int(x/10)) if x != 0 else -1,
                ] for x in fstats[segment][row]])
            tmp_fstats.append(tmp_row)
        aln.feature_stats = tmp_fstats

    def setup_alignments_from_selection(self, positive_selection, negative_selection):
        """
        The function gathers necessary information from provided selections
        and runs the calculations of the sequence alignments independently for
        both protein sets. It also finds the common set of residue positions.

        Arguments:
            positive_selection {Selection} -- selection containing first group of proteins
            negative_selection {[type]} -- selction containing second group of proteins along with the user-selcted sequence segments for the alignment
        """

        self.aln_pos.load_proteins_from_selection(positive_selection)
        self.aln_neg.load_proteins_from_selection(negative_selection)

        # local segment list
        segments = []

        # read selection
        for segment in negative_selection.segments:
            segments.append(segment)

        self.setup_alignments(segments)

    def calculate_signature(self):
        """
        Calculates the feature frequency difference between two protein sets.
        Generates the full differential matrix as well as maximum difference for a position (for scatter plot).
        """
        for sid, segment in enumerate(self.aln_neg.segments):
            self.features_normalized_pos[segment] = np.array(
                [[x[0] for x in feat[sid]] for feat in self.aln_pos.feature_stats],
                dtype='int'
                )
            self.features_normalized_neg[segment] = np.array(
                [[x[0] for x in feat[sid]] for feat in self.aln_neg.feature_stats],
                dtype='int'
                )

        for segment in self.aln_neg.segments:
            #TODO: get the correct default numering scheme from settings
            for idx, res in enumerate(self.common_gn[self.common_schemes[0][0]][segment].keys()):
                if res not in self.aln_pos.generic_numbers[self.common_schemes[0][0]][segment].keys():
                    self.features_normalized_pos[segment] = np.insert(self.features_normalized_pos[segment], idx, self.default_column, axis=1)
                elif res not in self.aln_neg.generic_numbers[self.common_schemes[0][0]][segment].keys():
                    self.features_normalized_neg[segment] = np.insert(self.features_normalized_neg[segment], idx, self.default_column, axis=1)

            # now the difference
            self.features_frequency_difference[segment] = np.subtract(
                self.features_normalized_pos[segment],
                self.features_normalized_neg[segment]
                )

        self._convert_feature_stats(self.features_normalized_pos, self.aln_pos)
        self._convert_feature_stats(self.features_normalized_neg, self.aln_neg)

        # Version with display data
        for row in range(len(AMINO_ACID_GROUPS.keys())):
            tmp_row = []
            for segment in self.aln_neg.segments:
                #first item is the real value,
                # second is the assignmnent of color (via css)
                # 0 - red, 5 - yellow, 10 - green
                #third item is a tooltip
                tmp_row.append([[
                    x,
                    int(x/20)+5 if x!= 0 else -1,
                    "{} - {}".format(
                        self.features_normalized_pos[segment][row][y],
                        self.features_normalized_neg[segment][row][y]
                        )
                    ] for y, x in enumerate(self.features_frequency_difference[segment][row])])
            self.features_frequency_diff_display.append(tmp_row)

        self.signature = OrderedDict([(x, []) for x in self.aln_neg.segments])
        for segment in self.aln_neg.segments:
            tmp = np.array(self.features_frequency_difference[segment])
            #signature_map = np.absolute(tmp).argmax(axis=0)
            signature_map = tmp.argmax(axis=0)
            # Update mapping to prefer features with fewer amino acids
            signature_map = self._assign_preferred_features(signature_map, segment, self.features_frequency_difference)

            self.signature[segment] = []
            for col, pos in enumerate(list(signature_map)):
                self.signature[segment].append([
                    list(AMINO_ACID_GROUPS.keys())[pos],
                    list(AMINO_ACID_GROUP_NAMES.values())[pos],
                    self.features_frequency_difference[segment][pos][col],
                    int(self.features_frequency_difference[segment][pos][col]/20)+5
                ])

        features_pos = OrderedDict()
        features_neg = OrderedDict()
        self.features_consensus_pos = OrderedDict([(x, []) for x in self.aln_neg.segments])
        self.features_consensus_neg = OrderedDict([(x, []) for x in self.aln_neg.segments])
        for sid, segment in enumerate(self.aln_neg.segments):
            features_pos[segment] = np.array(
                [[x[0] for x in feat[sid]] for feat in self.aln_pos.feature_stats],
                dtype='int'
                )
            features_neg[segment] = np.array(
                [[x[0] for x in feat[sid]] for feat in self.aln_neg.feature_stats],
                dtype='int'
                )
            features_cons_pos = features_pos[segment].argmax(axis=0)
            features_cons_pos = self._assign_preferred_features(features_cons_pos, segment, features_pos)
            features_cons_neg = features_neg[segment].argmax(axis=0)
            features_cons_neg = self._assign_preferred_features(features_cons_neg, segment, features_neg)

            for col, pos in enumerate(list(features_cons_pos)):
                self.features_consensus_pos[segment].append([
                    list(AMINO_ACID_GROUPS.keys())[pos],
                    list(AMINO_ACID_GROUP_NAMES.values())[pos],
                    features_pos[segment][pos][col],
                    int(features_pos[segment][pos][col]/20)+5
                ])
            for col, pos in enumerate(list(features_cons_neg)):
                self.features_consensus_neg[segment].append([
                    list(AMINO_ACID_GROUPS.keys())[pos],
                    list(AMINO_ACID_GROUP_NAMES.values())[pos],
                    features_neg[segment][pos][col],
                    int(features_neg[segment][pos][col]/20)+5
                ])
        self._convert_feature_stats(self.features_normalized_pos, self.aln_pos)
        self._convert_feature_stats(self.features_normalized_neg, self.aln_neg)

    def prepare_display_data(self):

        options = {
            'num_residue_columns': len(sum([[x for x in self.common_gn[self.common_schemes[0][0]][segment]] for segment in self.aln_neg.segments], [])),
            'num_of_sequences_pos': len(self.aln_pos.proteins),
            'num_residue_columns_pos': len(self.aln_pos.positions),
            'num_of_sequences_neg': len(self.aln_neg.proteins),
            'num_residue_columns_neg': len(self.aln_neg.positions),
            'common_segments': self.common_segments,
            'common_generic_numbers': self.common_gn,
            'feats_signature': self.features_frequency_diff_display,
            'signature_consensus': self.signature,
            'feats_cons_pos': self.features_consensus_pos,
            'feats_cons_neg': self.features_consensus_neg,
            'a_pos': self.aln_pos,
            'a_neg': self.aln_neg,
        }
        return options

    def prepare_session_data(self):

        session_signature = {
            'common_positions': self.common_gn,
            'diff_matrix': self.features_frequency_difference,
            'numbering_schemes': self.common_schemes,
            'common_segments': self.common_segments,
        }
        return session_signature

    def merge_numbering_schemes(self):
        """
        Extract all of the numbering schemes used for a set of proteins.

        Arguments:
            proteins {selection} -- A set of proteins to analyze
        """

        numbering_schemes = {}
        for prot in self.aln_pos.proteins + self.aln_neg.proteins:
            if prot.protein.residue_numbering_scheme.slug not in numbering_schemes:
                rnsn = prot.protein.residue_numbering_scheme.name
                numbering_schemes[prot.protein.residue_numbering_scheme.slug] = rnsn
        # order and convert numbering scheme dict to tuple
        return sorted(numbering_schemes.items(), key=lambda x: x[0])

    # def apply_cutoff(self, cutoff=0):

    #     matrix_consensus = OrderedDict()
    #     for segment in self.segments:
    #         # print(segment)
    #         segment_consensus = []
    #         signature_map = np.absolute(self.features_frequency_difference[segment]).argmax(axis=0)
    #         for col, pos in enumerate(list(signature_map)):
    #             if abs(self.features_frequency_difference[segment][pos][col]) > self.cutoff:
    #                 segment_consensus.append(self.features_frequency_difference[segment][ : , col])
    #                 for scheme in self.schemes:
    #                     gnum = list(self.common_gn[scheme[0]][segment].items())[col]
    #                     try:
    #                         self.relevant_gn[scheme[0]][segment][gnum[0]] = gnum[1]
    #                     except:
    #                         self.relevant_gn[scheme[0]][segment] = OrderedDict()
    #                         self.relevant_gn[scheme[0]][segment][gnum[0]] = gnum[1]

    #         segment_consensus = np.array(segment_consensus).T
    #         if segment_consensus != []:
    #             matrix_consensus[segment] = segment_consensus
    #     self.signature_matrix_filtered = matrix_consensus
    #     self.relevant_segments = OrderedDict([
    #         (
    #             x[0],
    #             self.relevant_gn[self.schemes[0][0]][x[0]].keys()
    #         ) for x in self.signature_matrix_filtered.items()
    #     ])
    #     signature = OrderedDict([(x[0], []) for x in matrix_consensus.items()])
    #     for segment in self.relevant_segments:
    #         signature_map = np.absolute(self.signature_matrix_filtered[segment]).argmax(axis=0)
    #         tmp = np.array(self.signature_matrix_filtered[segment])
    #         for col, pos in enumerate(list(signature_map)):
    #             signature[segment].append([
    #                 list(AMINO_ACID_GROUPS.keys())[pos],
    #                 list(AMINO_ACID_GROUP_NAMES.values())[pos],
    #                 tmp[pos][col],
    #                 int(tmp[pos][col]/20)+5
    #             ])
    #     self.signature_consensus = signature

    def prepare_excel_worksheet(self, workbook, worksheet_name, aln='positive', data='alignment'):
        """
        A function saving alignment data subset into the excel spreadsheet.
        It adds a worksheet to an existing workbook and saves only a selected subset of alignment data.
        For a complete save of the alignment it needs to be wrapped with additional code.

        The outline of the excel worksheet is similar to the one of html page.
        First column shows nunbering schemes, protein list, etc
        The frequency data start from column B

        Arguments:
            workbook {xlrsxwriter.Workbook} -- object to add workseet to
            worksheet_name {string} -- name for the new workseet

        Keyword Arguments:
            alignment {string} -- alignment to extract data from.
                                    Possible choices: positive, negative, signature
            data {string} -- data type to save to workshet: 'alignment' or 'features' frequencies
        """

        props = AMINO_ACID_GROUP_NAMES.values()
        worksheet = workbook.add_worksheet(worksheet_name)

        if aln == 'positive':
            alignment = self.aln_pos
            if data == 'features':
                data_block = self.aln_pos.feature_stats
                feat_consensus = self.features_consensus_pos
        elif aln == 'negative':
            alignment = self.aln_neg
            if data == 'features':
                data_block = self.aln_neg.feature_stats
                feat_consensus = self.features_consensus_neg
        else:
            if data == 'features':
                data_block = self.features_frequency_diff_display
                feat_consensus = self.signature
        numbering_schemes = self.common_schemes
        generic_numbers_set = self.common_gn

        # First column, numbering schemes
        for row, scheme in enumerate(numbering_schemes):
            worksheet.write(1 + 3*row, 0, scheme[1])

        # First column, stats
        if data == 'features':
            for offset, prop in enumerate(props):
                worksheet.write(1 + 3 * len(numbering_schemes) + offset, 0, prop)

        # First column, protein list (for alignment) and line for consensus sequence
        else:
            for offset, prot in enumerate(alignment.proteins):
                worksheet.write(
                    1 + 3 * len(numbering_schemes) + offset,
                    0,
                    prot.protein.entry_name
                )
            worksheet.write(
                1 + 3 * len(numbering_schemes) + len(alignment.proteins),
                0,
                'CONSENSUS'
                )

        # Second column and on
        # Segments
        offset = 0
        for segment in generic_numbers_set[numbering_schemes[0][0]].keys():
            worksheet.merge_range(
                0,
                1 + offset,
                0,
                len(generic_numbers_set[numbering_schemes[0][0]][segment]) + offset - 1,
                segment
            )
            offset += len(generic_numbers_set[numbering_schemes[0][0]][segment])

        # Generic numbers
        # for row, item in enumerate(generic_numbers_set.items()):
        for row, item in enumerate(numbering_schemes):
            scheme = item[0]
            offset = 1
            for sn, gn_list in generic_numbers_set[scheme].items():
                for col, gn_pair in enumerate(gn_list.items()):
                    try:
                        tm, bw, gpcrdb = re.split('\.|x', strip_html_tags(gn_pair[1]))
                    except:
                        tm, bw, gpcrdb = ('', '', '')
                    worksheet.write(
                        1 + 3 * row,
                        col + offset,
                        tm
                    )
                    worksheet.write(
                        2 + 3 * row,
                        col + offset,
                        bw
                    )
                    worksheet.write(
                        3 + 3*row,
                        col + offset,
                        gpcrdb
                    )
                offset += len(gn_list.items())

        # Stats
        if data == 'features':
            offset = 1 + 3 * len(numbering_schemes)

            for row, prop in enumerate(data_block):
                col_offset = 0
                for segment in prop:
                    for col, freq in enumerate(segment):
                        cell_format = workbook.add_format(get_format_props(freq[1]))
                        worksheet.write(
                            offset + row,
                            1 + col + col_offset,
                            freq[0] if isinstance(freq[0], int) else int(freq[0]),
                            cell_format
                        )
                    col_offset += len(segment)
            col_offset = 0
            for segment, cons_feat in feat_consensus.items():
                for col, chunk in enumerate(cons_feat):
                    worksheet.write(
                        offset + len(AMINO_ACID_GROUPS),
                        1 + col + col_offset,
                        chunk[0]
                    )
                    cell_format = workbook.add_format(get_format_props(int(chunk[2]/20)+5) if chunk[2] != 0 else get_format_props(-1))
                    worksheet.write(
                        1 + offset + len(AMINO_ACID_GROUPS),
                        1 + col + col_offset,
                        chunk[2],
                        cell_format
                    )
                col_offset += len(cons_feat)
        # Alignment
        else:
            offset = 1 + 3 * len(alignment.numbering_schemes)

            for row, data in enumerate(alignment.proteins):
                col_offset = 0
                for segment, sequence in data.alignment.items():
                    for col, res in enumerate(sequence):
                        cell_format = workbook.add_format(get_format_props(res=res[2]))
                        worksheet.write(
                            offset + row,
                            1 + col + col_offset,
                            res[2],
                            cell_format
                        )
                    col_offset += len(sequence)
            # Consensus sequence
            row = 1 + 3 * len(alignment.numbering_schemes) + len(alignment.proteins)
            col_offset = 0
            for segment, sequence in alignment.consensus.items():
                for col, data in enumerate(sequence.items()):
                    res = data[1]
                    cell_format = workbook.add_format(get_format_props(res=res[0]))
                    worksheet.write(
                        row,
                        1 + col + col_offset,
                        res[0],
                        cell_format
                    )
                    cell_format = workbook.add_format(get_format_props(res[1]))
                    worksheet.write(
                        row + 1,
                        1 + col + col_offset,
                        res[2],
                        cell_format
                    )

                col_offset += len(sequence.items())


class SignatureMatch():

    def __init__(self, common_positions, numbering_schemes, segments, difference_matrix, protein_set, cutoff=40):

        self.cutoff = cutoff
        self.common_gn = common_positions
        self.schemes = numbering_schemes
        self.segments = segments
        self.diff_matrix = difference_matrix
        self.signature_matrix_filtered = OrderedDict()
        self.signature_consensus = OrderedDict()
        self.protein_set = protein_set
        self.relevant_gn = OrderedDict([(x[0], OrderedDict()) for x in self.schemes])
        self.relevant_segments = OrderedDict()
        self.scored_proteins = []
        self.protein_report = OrderedDict()
        self.protein_signatures = OrderedDict()
        self.feature_preference = prepare_aa_group_preference()

        self.find_relevant_gns()

        self.residue_to_feat = dict(
            [(x, set()) for x in AMINO_ACIDS.keys()]
            )
        for fidx, feat in enumerate(AMINO_ACID_GROUPS.items()):
            for res in feat[1]:
                try:
                    self.residue_to_feat[res].add(fidx)
                except KeyError:
                    self.residue_to_feat['-'].add(fidx)

    def _assign_preferred_features(self, signature, segment, ref_matrix):

        new_signature = []
        for pos, argmax in enumerate(signature):
            updated = True
            new_feat = argmax
            while updated:
                tmp = self._calculate_best_feature(pos, segment, argmax, ref_matrix)
                if tmp == new_feat:
                    updated = False
                else:
                    new_feat = tmp
            new_signature.append(new_feat)
        return new_signature


    def _calculate_best_feature(self, pos, segment, argmax, ref_matrix):

        tmp = self.feature_preference[argmax]
        amax = ref_matrix[segment][argmax, pos]
        equiv_feat = np.where(np.isin(ref_matrix[segment][:, pos], amax))[0]
        for efeat in equiv_feat:
            if efeat in tmp:
                return efeat
        return argmax

    def find_relevant_gns(self):
        """
        Find the set of generic residue positions meeting the cutoff.
        """

        matrix_consensus = OrderedDict()
        for segment in self.segments:
            segment_consensus = []
            signature_map = self.diff_matrix[segment].argmax(axis=0)
            # Update mapping to prefer features with fewer amino acids
            signature_map = self._assign_preferred_features(signature_map, segment, self.diff_matrix)
            for col, pos in enumerate(list(signature_map)):
                if self.diff_matrix[segment][pos][col] >= self.cutoff:
                    segment_consensus.append(self.diff_matrix[segment][ : , col])
                    for scheme in self.schemes:
                        gnum = list(self.common_gn[scheme[0]][segment].items())[col]
                        try:
                            self.relevant_gn[scheme[0]][segment][gnum[0]] = gnum[1]
                        except KeyError:
                            self.relevant_gn[scheme[0]][segment] = OrderedDict()
                            self.relevant_gn[scheme[0]][segment][gnum[0]] = gnum[1]
            segment_consensus = np.array(segment_consensus).T

            if segment_consensus.shape != (0,):
                matrix_consensus[segment] = segment_consensus
        self.signature_matrix_filtered = matrix_consensus
        self.relevant_segments = OrderedDict([
            (
                x[0],
                self.relevant_gn[self.schemes[0][0]][x[0]].keys()
            ) for x in self.signature_matrix_filtered.items()
        ])

        signature = OrderedDict([(x[0], []) for x in matrix_consensus.items()])
        for segment in self.relevant_segments:
            signature_map = self.signature_matrix_filtered[segment].argmax(axis=0)
            signature_map = self._assign_preferred_features(signature_map, segment, self.signature_matrix_filtered)
            tmp = np.array(self.signature_matrix_filtered[segment])

            for col, pos in enumerate(list(signature_map)):
                signature[segment].append([
                    list(AMINO_ACID_GROUPS.keys())[pos],
                    list(AMINO_ACID_GROUP_NAMES.values())[pos],
                    tmp[pos][col],
                    int(tmp[pos][col]/20)+5
                ])
        self.signature_consensus = signature


    def score_protein_class(self, pclass_slug='001'):

        start = time.time()
        protein_scores = {}
        protein_signature_match = {}
        class_proteins = Protein.objects.filter(
            species__common_name='Human',
            family__slug__startswith=pclass_slug
            ).exclude(
                id__in=[x.id for x in self.protein_set]
                )
        class_a_pcf = ProteinConformation.objects.order_by(
            'protein__family__slug',
            'protein__entry_name'
            ).filter(
                protein__in=class_proteins,
                protein__sequence_type__slug='wt'
                ).exclude(protein__entry_name__endswith='-consensus')
        for pcf in class_a_pcf:
            p_start = time.time()
            score, nscore, signature_match = self.score_protein(pcf)
            protein_scores[pcf] = (score, nscore)
            protein_signature_match[pcf] = signature_match
            p_end = time.time()
            print("Time elapsed for {}: ".format(pcf.protein.entry_name), p_end - p_start)
        end = time.time()
        self.protein_report = OrderedDict(sorted(protein_scores.items(), key=lambda x: x[1][0], reverse=True))
        for prot in self.protein_report.items():
            self.protein_signatures[prot[0]] = protein_signature_match[prot[0]]
        self.scored_proteins = list(self.protein_report.keys())
        print("Total time: ", end - start)


    def score_protein(self, pcf):
        prot_score = 0.0
        norm = 0.0
        consensus_match = OrderedDict([(x, []) for x in self.relevant_segments])

        relevant_gns_total = []
        for segment in  self.relevant_segments:
            for idx, pos in enumerate(self.relevant_gn[self.schemes[0][0]][segment].keys()):
                relevant_gns_total.append(pos)

        resi = Residue.objects.filter(
            protein_conformation=pcf,
            generic_number__label__in=relevant_gns_total
            ).prefetch_related('generic_number')
        resi_dict = {}
        for r in resi:
            if r.generic_number:
                resi_dict[r.generic_number.label] = r

        for segment in self.relevant_segments:
            tmp = []
            signature_map = self.signature_matrix_filtered[segment].argmax(axis=0)
            signature_map = self._assign_preferred_features(signature_map, segment, self.signature_matrix_filtered)

            norm += np.sum(np.amax(self.signature_matrix_filtered[segment], axis=0))

            for idx, pos in enumerate(self.relevant_gn[self.schemes[0][0]][segment].keys()):
                feat = signature_map[idx]
                feat_abr = list(AMINO_ACID_GROUPS.keys())[feat]
                feat_name = list(AMINO_ACID_GROUP_NAMES.values())[feat]
                val = self.signature_matrix_filtered[segment][feat][idx]
                if pos in resi_dict:
                    res = resi_dict[pos]
                    if feat in self.residue_to_feat[res.amino_acid]:
                        if val > 0:
                            prot_score += val
                        tmp.append([
                            feat_abr,
                            feat_name,
                            val,
                            "green",
                            res.amino_acid, pos
                            ]) if val > 0 else tmp.append([
                                feat_abr,
                                feat_name,
                                val,
                                "white",
                                res.amino_acid,
                                pos
                                ])
                    else:
                        #David doesn't want the negative values in the score
                        # prot_score -= val
                        tmp.append([
                            feat_abr,
                            feat_name,
                            val,
                            "red",
                            res.amino_acid,
                            pos
                            ]) if val > 0 else tmp.append([
                                feat_abr,
                                feat_name,
                                val,
                                "white",
                                res.amino_acid,
                                pos
                                ])
                else:
                    if feat_name == 'Gap':
                        tmp.append([
                            feat_abr,
                            feat_name,
                            val,
                            "green",
                            '_',
                            pos
                            ]) if val > 0 else tmp.append([
                                feat_abr,
                                feat_name,
                                val,
                                "white",
                                '_',
                                pos
                                ])
                        prot_score += val
                    else:
                        #David doesn't want the negative values in the score
                        #prot_score -= val
                        tmp.append([
                            feat_abr,
                            feat_name,
                            val,
                            "red",
                            '_',
                            pos
                            ]) if val > 0 else tmp.append([
                                feat_abr,
                                feat_name,
                                val,
                                "white",
                                '_',
                                pos
                                ])
            consensus_match[segment] = tmp
        return (prot_score/100, prot_score/norm*100, consensus_match)

def signature_score_excel(workbook, scores, protein_signatures, signature_filtered, relevant_gn, relevant_segments, numbering_schemes):

    worksheet = workbook.add_worksheet('scored_proteins')
    #wrap = workbook.add_format({'text_wrap': True})


    # First column, numbering schemes
    for row, scheme in enumerate(numbering_schemes):
        worksheet.write(1 + 3*row, 0, scheme[1])
    worksheet.write(1,1, 'Receptor family / Ligand class')
    # Score header
    worksheet.write(1, 2, 'Score')
    worksheet.write(1, 3, 'Normalized score')

    offset = 0
    # Segments
    for segment, resi in relevant_segments.items():
        worksheet.merge_range(
            0,
            4 + offset,
            0,
            4 + len(resi) + offset - 1,
            segment
        )
        offset += len(resi)

    # Generic numbers
    # for row, item in enumerate(generic_numbers_set.items()):
    for row, item in enumerate(numbering_schemes):
        scheme = item[0]
        offset = 1
        for sn, gn_list in relevant_gn[scheme].items():
            for col, gn_pair in enumerate(gn_list.items()):
                try:
                    tm, bw, gpcrdb = re.split('\.|x', strip_html_tags(gn_pair[1]))
                except:
                    tm, bw, gpcrdb = ('', '', '')
                worksheet.write(
                    1 + 3 * row,
                    3 + col + offset,
                    tm
                )
                worksheet.write(
                    2 + 3 * row,
                    3 + col + offset,
                    bw
                )
                worksheet.write(
                    3 + 3*row,
                    3 + col + offset,
                    gpcrdb
                )
            offset += len(gn_list.items())

    # Line for sequence signature
    worksheet.write(
        1 + 3 * len(numbering_schemes),
        0,
        'CONSENSUS'
        )
    col_offset = 0
    for segment, cons_feat in signature_filtered.items():
        for col, chunk in enumerate(cons_feat):
            worksheet.write(
                1 + 3 * len(numbering_schemes),
                4 + col + col_offset,
                chunk[0]
            )
            cell_format = workbook.add_format(get_format_props(int(chunk[2]/20)+5))
            worksheet.write(
                2 + 3 * len(numbering_schemes),
                4 + col + col_offset,
                chunk[2],
                cell_format
            )
        col_offset += len(cons_feat)



    # Score lines
    row_offset = 0
    for protein, score in scores.items():
        worksheet.write(
            3 + 3 * len(numbering_schemes) + row_offset,
            0,
            protein.protein.entry_name,
        )
        worksheet.write(
            3 + 3 * len(numbering_schemes) + row_offset,
            1,
            "{} / {}".format(
                protein.protein.family.parent.parent.name,
                protein.protein.family.parent.name
            )
        )
        worksheet.write(
            3 + 3 * len(numbering_schemes) + row_offset,
            2,
            score[0],
        )
        worksheet.write(
            3 + 3 * len(numbering_schemes) + row_offset,
            3,
            score[1],
        )
        col_offset = 0
        for segment, data in protein_signatures[protein].items():
            for col, res in enumerate(data):
                cell_format = workbook.add_format({'bg_color': res[3],})
                worksheet.write(
                    3 + 3 * len(numbering_schemes) + row_offset,
                    4 + col + col_offset,
                    res[4],
                    cell_format
                )
            col_offset += len(data)
        row_offset += 1

def prepare_aa_group_preference():

    pref_dict = {}
    lengths = {}
    for row, group in enumerate(AMINO_ACID_GROUPS.items()):
        tmp_len = len(group[1])
        try:
            lengths[tmp_len].append(row)
        except KeyError:
            lengths[tmp_len] = [row,]
    l_heap = sorted(lengths.keys())
    while l_heap:
        tmp = l_heap.pop()
        for feat_row in lengths[tmp]:
            pref_dict[feat_row] = []
            for pref_feat in l_heap:
                pref_dict[feat_row].extend(lengths[pref_feat])
    return pref_dict