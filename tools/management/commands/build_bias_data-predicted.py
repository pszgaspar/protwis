from decimal import Decimal
import logging
import math
import pandas as pd
import os
from build.management.commands.base_build import Command as BaseBuild
from protein.models import ProteinGProteinPair
from ligand.models import BiasedExperiment, AnalyzedExperiment, AnalyzedAssay
from django.conf import settings


class Command(BaseBuild):
    mylog = logging.getLogger(__name__)
    mylog.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s:%(levelname)s:%(message)s')
    file_handler = logging.FileHandler('biasDataTest.log')
    file_handler.setLevel(logging.ERROR)
    file_handler.setFormatter(formatter)
    mylog.addHandler(file_handler)
    structure_data_dir = os.sep.join(
        [settings.DATA_DIR, 'ligand_data', 'gproteins'])
    help = 'Reads bias data and imports it'
    publication_cache = {}
    gprot_cache = {}
    data_all = []
    endogenous_assays = list()

    def add_arguments(self, parser):
        parser.add_argument('-p', '--proc',
                            type=int,
                            action='store',
                            dest='proc',
                            default=1,
                            help='Number of processes to run')
        parser.add_argument('-f', '--filename',
                            action='append',
                            dest='filename',
                            help='Filename to import. Can be used multiple times')
        parser.add_argument('-u', '--purge',
                            action='store_true',
                            dest='purge',
                            default=False,
                            help='Purge existing bias records')
        parser.add_argument('--test_run', action='store_true', help='Skip this during a test run',
                            default=False)
        self.logger.info('COMPLETED CREATING BIAS DATA')

    def handle(self, *args, **options):
        if options['test_run']:
            print('Skipping in test run')
            return
        # delete any existing structure data
        if options['purge']:
            try:
                print('Started purging bias data')
                self.purge_bias_data()
                print('Ended purging bias data')
            except Exception as msg:
                print(msg)
        print('CREATING BIAS DATA')
        print(options['filename'])
        self.build_bias_data()

        self.logger.info('COMPLETED CREATING BIAS DATA')

    def purge_bias_data(self):
        delete_bias_experiment = AnalyzedExperiment.objects.all()
        delete_bias_experiment.delete()
        self.logger.info('Data is purged')

    def process_gproteins_excel(self):
        source_file_path = None
        filenames = os.listdir(self.structure_data_dir)
        for source_file in filenames:
            source_file_path = os.sep.join(
                [self.structure_data_dir, source_file]).replace('//', '/')
            print(source_file, source_file_path)
        df = pd.read_excel(source_file_path)
        self.gprot_cache = df.set_index('UniProt').T.to_dict('dict')

    def build_bias_data(self):
        print('prestage, process excell')
        self.process_gproteins_excel()
        print('Build bias data gproteins')
        context = dict()
        content = self.get_from_model()
        print('stage # 1 : Getting data finished, data points: ', len(content))
        content_with_children = self.process_data(content)
        print('stage # 2: Processing children in queryset finished',
              len(content_with_children))
        changed_data = self.queryset_to_dict(content_with_children)
        print('stage # 3: Converting queryset into dict finished', len(changed_data))
        send = self.combine_unique(changed_data)
        print('stage # 4: Converting queryset into dict finished', len(changed_data))
        self.get_endogenous_assays(send)
        print('stage # 5: Selecting endogenous ligands finished')
        referenced_assay = self.process_referenced_assays(send)
        print('stage # 6: Separating reference assays is finished',
              len(referenced_assay))
        ligand_data = self.separate_ligands(referenced_assay)
        print('stage # 7: Separate ligands finished')
        limit_family = self.process_signalling_proteins(ligand_data)
        print('stage # 8: process_signalling_proteins finished', len(limit_family))
        calculated_assay = self.process_calculation(limit_family)
        print('stage # 9: Calucating finished')
        self.count_publications(calculated_assay)
        print('stage # 10: labs and publications counted')
        context.update({'data': calculated_assay})
        print('stage # 11: combining data into common dict is finished')
        # save dataset to model
        self.save_data_to_model(context, 'predicted_family')
        print('stage # 12: saving data to model is finished')

    def get_from_model(self):
        try:
            content = BiasedExperiment.objects.all().prefetch_related(
                'experiment_data', 'ligand', 'receptor', 'publication', 'publication__web_link', 'experiment_data__emax_ligand_reference',
            ).order_by('publication', 'receptor', 'ligand')
        except BiasedExperiment.DoesNotExist:
            self.logger.info('Data is not returned')
            content = None
        return content

    def process_data(self, content):
        rd = []
        counter = 0
        for instance in enumerate(content):
            temp_obj = []
            fin_obj = {}
            fin_obj['main'] = (instance[1])
            vendor_counter = 0
            vendors_quantity = None
            for i in instance[1].experiment_data_vendors.all():
                vendor_counter = vendor_counter + 1
                if not vendor_counter:
                    vendors_quantity = i
                    self.logger.info(vendors_quantity)

            for entry in instance[1].experiment_data.all():
                author_list = list()
                for author in entry.experiment_data_authors.all():
                    author_list.append(author.author)
                temp_obj.append(entry)
                counter += 1
            fin_obj['authors'] = author_list
            fin_obj['children'] = temp_obj
            fin_obj['vendor_counter'] = vendor_counter
            rd.append(fin_obj)
        self.logger.info('Return dict is returned')
        return rd

    def process_g_protein(self, protein, receptor):
        receptor_name = receptor.entry_name.split('_')[0].upper()
        if receptor_name in self.gprot_cache:
            protein = self.gprot_cache[receptor_name]["1'Gfam"]
        return protein

    def queryset_to_dict(self, results):
        send = list()
        for j in results:
            temp_dict = dict()
            temp = dict()
            temp['reference'] = list()
            temp['assay'] = dict()
            temp['ref_ligand_experiment'] = dict()
            doubles = []
            temp['publication'] = j['main'].publication
            temp['species'] = j['main'].receptor.species.common_name
            # temp['ligand'] = j['main'].ligand
            temp['endogenous_ligand'] = j['main'].endogenous_ligand
            temp['receptor'] = j['main'].receptor
            temp['vendor_counter'] = j['vendor_counter']
            temp['authors'] = j['authors']
            temp['article_quantity'] = 0
            temp['labs_quantity'] = 0
            temp['reference_ligand'] = None
            if not j['children']:
                continue
            temp_dict['receptor'] = j['main'].receptor
            temp_dict['assay_id'] = j['children'][0].id
            temp_dict['ligand_source_id'] = j['main'].ligand_source_id
            temp_dict['ligand_source_type'] = j['main'].ligand_source_type
            temp_dict['potency'] = ''
            temp_dict['t_factor'] = ''
            temp_dict['log_bias_factor'] = ''
            temp_dict['lbf_a'] = None
            temp_dict['lbf_b'] = None
            temp_dict['lbf_c'] = None
            temp_dict['lbf_d'] = None
            temp_dict['order_no'] = 0
            temp_dict['order_bias_value'] = None
            temp_dict['reference_ligand'] = list()
            temp_dict['signalling_protein'] = j['children'][0].signalling_protein.lower()
            temp_dict['cell_line'] = j['children'][0].cell_line
            temp_dict['family'] = j['children'][0].family
            if temp_dict['family'] == 'G protein' or temp_dict['family'] == 'Gq/11 or Gi/o':
                temp_dict['family'] = self.process_g_protein(
                    temp_dict['family'], temp['receptor'])
            if temp_dict['family'] == 'G protein' or temp_dict['family'] == 'Gq/11 or Gi/o':
                continue
            temp_dict['measured_biological_process'] = j['children'][0].measured_biological_process
            temp_dict['assay_type'] = j['children'][0].assay_type

            temp_dict['assay_time_resolved'] = j['children'][0].assay_time_resolved
            temp_dict['signal_detection_tecnique'] = j['children'][0].signal_detection_tecnique

            if j['children'][0].quantitive_activity:
                temp_dict['quantitive_activity'] = j['children'][0].quantitive_activity
                temp_dict['quantitive_activity_initial'] = j['children'][0].quantitive_activity
            else:
                temp_dict['quantitive_activity'] = None
                temp_dict['quantitive_activity_initial'] = None

            temp_dict['molecule_1'] = j['children'][0].molecule_1
            temp_dict['molecule_2'] = j['children'][0].molecule_2
            temp_dict['qualitative_activity'] = j['children'][0].qualitative_activity
            temp_dict['quantitive_unit'] = j['children'][0].quantitive_unit
            temp_dict['quantitive_efficacy'] = j['children'][0].quantitive_efficacy
            temp_dict['efficacy_unit'] = j['children'][0].efficacy_unit
            temp_dict['quantitive_measure_type'] = j['children'][0].quantitive_measure_type
            temp_dict['efficacy_measure_type'] = j['children'][0].efficacy_measure_type
            temp_dict['t_coefficient'] = j['children'][0].bias_value
            temp_dict['t_coefficient_initial'] = j['children'][0].bias_value_initial
            temp_dict['bias_reference'] = j['children'][0].bias_reference
            temp_dict['emax_reference_ligand'] = j['children'][0].emax_ligand_reference
            temp_dict['ligand_function'] = j['children'][0].ligand_function
            temp_dict['ligand'] = j['main'].ligand
            if (temp_dict['quantitive_activity_initial'] and
                    temp_dict['quantitive_measure_type'] != "Effect at single point measurement"):
                temp_dict['quantitive_activity_initial'] = (-1) * math.log10(
                    temp_dict['quantitive_activity_initial'])
                temp_dict['quantitive_activity_initial'] = "{:.2F}".format(
                    Decimal(temp_dict['quantitive_activity_initial']))
            temp['ref_ligand_experiment'] = j['children'][0].emax_ligand_reference
            doubles.append(temp_dict)
            temp['assay'] = doubles
            send.append(temp)
        self.logger.info('Queryset processed')
        return send

    def get_endogenous_assays(self, data):
        # TODO: temporarily not used in main browser
        for experiment in data.items():
            for assay in experiment[1]['assay']:
                if assay['bias_reference'] == 'Endogenous' or assay['bias_reference'] == 'Ref. and endo.':
                    self.endogenous_assays.append(assay)

    def combine_unique(self, data):
        '''
        combining tested assays and reference assays
        '''
        context = dict()
        for j in data:
            name = str(j['publication'].id) + \
                '/' + '/' + str(j['receptor'].id)
            temp_obj = list()
            if name in context:
                temp_obj = context[name]['assay']
            for i in j['assay']:
                temp_obj.append(i)
            context[name] = j
            context[name]['assay'] = temp_obj
        self.logger.info('Combined experiments by publication and receptor')
        return context

    def process_referenced_assays(self, data):
        '''
        separate tested assays and reference assays
        '''
        for j in data.items():
            assays, reference = self.return_refenced_assays(j[1]['assay'])
            j[1].pop('assay')
            j[1]['assay_list'] = assays
            j[1]['reference_assays_list'] = reference
            self.logger.info('references processed')
        return data

    def return_refenced_assays(self, assays):
        # pylint: disable=no-member
        # no error
        main, reference = list(), list()
        for assay in assays:
            if assay['bias_reference'] == 'Endogenous' or assay['bias_reference'] == 'Ref. and endo.':
                reference.append(assay)

            elif assay['bias_reference'] == '':
                main.append(assay)
        sorted_main = sorted(main, key=lambda k: k['quantitive_activity']
                             if k['quantitive_activity'] else 999999, reverse=True)
        sorted_reference = sorted(reference, key=lambda k: k['quantitive_activity']
                                  if k['quantitive_activity'] else 999999, reverse=True)
        self.logger.info('Combined experiments by publication and receptor')
        return sorted_main, sorted_reference

    def get_endogenous_as_reference(self, main):
        reference_list = list()
        guest = main

        # required: match receptor and species
        temp_receptor = self.fetch_reference_from_endogenous_list(guest)
        temp_receptor = self.remove_dublicates(temp_receptor)
        for assay in guest['assay_list']:
            # required: match family
            temp_sp = self.fetch_reference_from_endogenous_list_signalling_protein(
                assay, temp_receptor)
            reference_list.extend(temp_sp)
        # remove dublicates

        reference_list = self.remove_dublicates(reference_list)
        # merge by ligands
        merged_content = self.count_endogenous_ligands(reference_list)
        # assign score for each ligand
        scored_content = self.score_limit_endogenous(merged_content)
        # select most scored ligand
        temp_list = self.select_best_assay(scored_content)
        try:
            temp_list = temp_list['data']
        except Exception:
            return None
        return temp_list

    def fetch_reference_from_endogenous_list(self, i):
        return list(filter(lambda assay: assay['receptor'] == i['receptor'], self.endogenous_assays))

    def fetch_reference_from_endogenous_list_signalling_protein(self, assay_for_processing, temp_receptor):
        endogenous_assay_list = list()
        # try:
        for endo_assay in temp_receptor:
            if assay_for_processing['family'] == endo_assay['family']:
                endogenous_assay_list.append(endo_assay)
                self.logger.info('returned finalised assay')
        return endogenous_assay_list
    # except:
    #     return endogenous_assay_list

    def remove_dublicates(self, reference_list):
        new_d = []
        for x in reference_list:
            if x not in new_d:
                new_d.append(x)
                self.logger.info('returned finalised assay')
        return new_d

    def count_endogenous_ligands(self, reference_list):
        ligands = set()
        content = dict()
        for assay in reference_list:
            ligands.add(assay['ligand'])
        if len(ligands) > 1:
            for assay in reference_list:
                name = assay['ligand']
                if name in content:
                    content[name]['data'].append(assay)
                    if content[name]['cell_line'] is None:
                        content[name]['cell_line'] = assay['cell_line']
                    if content[name]['measured_biological_process'] is None:
                        content[name]['measured_biological_process'] = assay['measured_biological_process']
                    if content[name]['molecule_1'] is None:
                        content[name]['molecule_1'] = assay['molecule_1']
                else:
                    content[name] = dict()
                    content[name]['data'] = list()
                    content[name]['score'] = 0
                    content[name]['data'].append(assay)
                    content[name]['molecule_1'] = assay['molecule_1']
                    content[name]['measured_biological_process'] = assay['measured_biological_process']
                    content[name]['cell_line'] = assay['cell_line']
        else:
            for assay in reference_list:
                name = assay['ligand']
                content[name] = dict()
                content[name]['data'] = list()
                content[name]['score'] = 0
                content[name]['data'].append(assay)
                content[name]['molecule_1'] = assay['molecule_1']
                content[name]['measured_biological_process'] = assay['measured_biological_process']
                content[name]['cell_line'] = assay['cell_line']
                self.logger.info('returned finalised assay')
        return content

    def score_limit_endogenous(self, content):
        for ligand in content.items():
            if ligand[1]['cell_line'] is not None:
                ligand[1]['score'] = ligand[1]['score'] + 1
            if ligand[1]['measured_biological_process'] is not None:
                ligand[1]['score'] = ligand[1]['score'] + 1
            if ligand[1]['molecule_1'] is not None:
                ligand[1]['score'] = ligand[1]['score'] + 1
            ligand[1]['score'] = ligand[1]['score'] + \
                (0.001 * len(ligand[1]['data']))
            ligand[1]['ligand_name'] = ligand[0]
            self.logger.info('returned finalised assay')
        return content

    def select_best_assay(self, content):
        return_best_list = dict()
        for i in content.items():
            if len(return_best_list) < 1:
                return_best_list = i[1]
            else:
                if i[1]['score'] > return_best_list['score']:
                    return_best_list = i[1]
                    self.logger.info('returned finalised assay')
        return return_best_list

    def separate_ligands(self, context):
        content = dict()
        for i in context.items():
            if(len(i[1]['reference_assays_list'])) < 1:
                for assay in i[1]['assay_list']:
                    name = str(i[1]['publication'].id) + \
                        '/' + str(assay['ligand'].id) + '/' + \
                        str(i[1]['receptor'].id)
                    if name in content:
                        content[name]['assay_list'].append(assay)

                    else:
                        content[name] = dict()
                        content[name]['assay_list'] = list()
                        content[name]['reference_assays_list'] = list()
                        content[name]['publication'] = i[1]['publication']
                        content[name]['ligand'] = assay['ligand']
                        content[name]['ligand_links'] = self.get_external_ligand_ids(
                            content[name]['ligand'])
                        # TODO: add external LigandStatistics
                        content[name]['endogenous_ligand'] = i[1]['endogenous_ligand']
                        content[name]['receptor'] = i[1]['receptor']
                        content[name]['vendor_counter'] = i[1]['vendor_counter']
                        content[name]['authors'] = i[1]['authors']
                        content[name]['article_quantity'] = i[1]['article_quantity']
                        content[name]['labs_quantity'] = i[1]['labs_quantity']
                        content[name]['assay_list'].append(assay)
                        # TODO: get endogenous as reference

                        content[name]['reference_ligand'] = None
                        content[name]['ligand_source_id'] = assay['ligand_source_id']
                        content[name]['ligand_source_type'] = assay['ligand_source_type']

        self.logger.info('returned finalised assay')
        return content

    def get_external_ligand_ids(self, ligand):
        ligand_list = list()
        try:
            for i in ligand.properities.web_links.all():
                ligand_list.append(
                    {'name': i.web_resource.name, "link": i.index})
        except:
            self.logger.info('get_external_ligand_ids')
        return ligand_list

    def process_signalling_proteins(self, context):
        for i in context.items():

            i[1]['assay_list'] = self.calculate_bias_factor_value(
                i[1]['assay_list'], i[1]['reference_assays_list'])

            i[1]['assay_list'] = self.sort_assay_list(i[1]['assay_list'])

            i[1]['assay_list'] = self.limit_family_set(i[1]['assay_list'])

            for item in enumerate(i[1]['assay_list']):
                item[1]['order_no'] = item[0]
        self.logger.info('process_signalling_proteins')
        return context

    def get_rid_of_gprot(self, assay, families, proteins):
        if assay['family'] == 'Gq/11 or Gi/o':
            compare_val_gio = None
            compare_val_gq = None
            if 'Gi/o' in proteins:
                compare_val_gio = next(
                    item for item in families if item["family"] == 'Gi/o')
            if 'Gq' in proteins:
                compare_val_gq = next(
                    item for item in families if item["family"] == 'Gq/11')
            if compare_val_gq is None and compare_val_gio is not None:
                try:
                    if assay['order_bias_value'] > compare_val_gio['order_bias_value']:
                        families[:] = [d for d in families if d.get(
                            'family') != compare_val_gio['family']]
                        assay['family'] = 'Gi/o'
                    else:
                        assay['family'] = 'skip'
                except:
                    assay['family'] = 'skip'
            elif compare_val_gio is None and compare_val_gq is not None:
                try:
                    if assay['order_bias_value'] > compare_val_gq['order_bias_value']:
                        families[:] = [d for d in families if d.get(
                            'family') != compare_val_gio['family']]
                        assay['family'] = 'Gq/11'
                    else:
                        assay['family'] = 'skip'
                except:
                    assay['family'] = 'skip'
            elif compare_val_gio is not None and compare_val_gq is not None:
                try:
                    if assay['order_bias_value'] > compare_val_gio['order_bias_value'] > compare_val_gq['order_bias_value']:
                        families[:] = [d for d in families if d.get(
                            'family') != compare_val_gq['family']]
                        assay['family'] = 'Gq/11'
                    elif assay['order_bias_value'] > compare_val_gq['order_bias_value'] > compare_val_gio['order_bias_value']:
                        families[:] = [d for d in families if d.get(
                            'family') != compare_val_gio['family']]
                        assay['family'] = 'Gi/o'
                    elif compare_val_gq['order_bias_value'] > assay['order_bias_value'] > compare_val_gio['order_bias_value']:
                        families[:] = [d for d in families if d.get(
                            'family') != compare_val_gio['family']]
                        assay['family'] = 'Gi/o'
                    elif compare_val_gio['order_bias_value'] > assay['order_bias_value'] > compare_val_gq['order_bias_value']:
                        families[:] = [d for d in families if d.get(
                            'family') != compare_val_gq['family']]
                        assay['family'] = 'Gq/11'
                except:
                    assay['family'] = 'skip'
            else:
                assay = None
            if assay and assay['family'] == 'skip':
                assay = None
                self.logger.info('get_rid_of_gprot')
        return assay

    def limit_family_set(self, assay_list):
        families = list()
        proteins = set()
        for assay in assay_list:
            if assay['family'] not in proteins:
                # if assay['family'] == 'Gq/11 or Gi/o':
                #     assay = self.get_rid_of_gprot(assay, families, proteins)
                if assay:
                    proteins.add(assay['family'])
                    families.append(assay)
            else:
                # if assay['family'] == 'Gq/11 or Gi/o':
                #     assay = self.get_rid_of_gprot(assay, families, proteins)
                if assay:
                    compare_val = next(
                        item for item in families if item["family"] == assay['family'])
                    try:
                        if assay['order_bias_value'] > compare_val['order_bias_value']:
                            families[:] = [d for d in families if d.get(
                                'family') != compare_val['family']]
                            families.append(assay)
                    except TypeError:
                        self.logger.info('skipping families if existing copy')

        return families

    def sort_assay_list(self, i):
        return_assay = dict()
        return_assay = sorted(i, key=lambda k: k['order_bias_value']
                              if k['order_bias_value'] else float(-1000), reverse=True)
        self.logger.info('sort_assay_list')
        return return_assay

    def calculate_bias_factor_value(self, sorted_assays, references):
        # TODO: pick
        for assay in sorted_assays:
            assay['order_bias_value'] = self.calc_order_bias_value(assay)
        return sorted_assays

    def calc_order_bias_value(self, assay):
        result = None
        try:
            assay_a = assay['quantitive_activity']
            assay_b = assay['quantitive_efficacy']
            result = math.log10(assay_b / assay_a)
        except:
            result = None
            self.logger.info('calc_order_bias_value')
        return result

    def process_calculation(self, context):
        for i in context.items():
            i[1]['biasdata'] = i[1]['assay_list']
            i[1].pop('assay_list')
            # calculate log bias
            self.calc_bias_factor(i[1]['biasdata'])
            # recalculates lbf if it is negative
            # i[1]['biasdata'] = self.validate_lbf(i)
            # self.calc_potency_and_transduction(i[1]['biasdata'])
            self.logger.info('process_calculation error')
        return context

    def calc_bias_factor(self, biasdata):

        most_potent = dict()
        for i in biasdata:
            if i['order_no'] == 0:
                most_potent = i
                try:
                    i['lbf_a'] = round(math.log10(
                        most_potent['quantitive_efficacy'] / most_potent['quantitive_activity']), 1)
                except:
                    i['log_bias_factor'] = None
                i['log_bias_factor'] = None
                self.process_low_potency(i)

        for i in biasdata:
            if i['order_no'] > 0:
                try:
                    i['log_bias_factor'] = self.lbf_process_ic50(i)
                    if i['log_bias_factor'] == None:
                        a = math.log10(
                            most_potent['quantitive_efficacy'] / most_potent['quantitive_activity'])
                        b = math.log10(
                            i['quantitive_efficacy'] / i['quantitive_activity'])
                        i['lbf_a'] = b
                        i['log_bias_factor'] = round(a - b, 1)
                except:
                    i['log_bias_factor'] = None
                i['t_factor'] = None
                self.logger.info('t_factor error')

    def lbf_process_qualitative_data(self, i):
        return_message = None
        try:
            if i['qualitative_activity'] == 'No activity':
                return_message = "Full Bias"
            elif i['qualitative_activity'] == 'Low activity':
                return_message = "High Bias"
            elif i['qualitative_activity'] == 'High activity':
                return_message = "Low Bias"
            elif i['qualitative_activity'] == 'Inverse agonism/antagonism':
                return_message = "Full Bias"
        except:
            return_message = None
            self.logger.info('lbf_process_qualitative_data')
        return return_message

    def lbf_process_efficacy(self, i):
        return_message = None
        try:
            if i['quantitive_efficacy'] == 0:
                return_message = "Full Bias"
        except:
            return_message = None
            self.logger.info('lbf_process_efficacy')
        return return_message

    def process_low_potency(self, i):
        try:
            if i['quantitive_activity_initial'] < 5 and i['quantitive_efficacy'] > 0:
                i['quantitive_activity'] == 12500 * (10**(-9))
        except:
            self.logger.info('get_rid_of_gprot')

    def lbf_calculate_bias(self, i, most_potent):
        return_message = None
        try:
            # TODO: possibly need mean for that one
            if (i['quantitive_measure_type'].lower() == 'ec50'
                    and most_potent['quantitive_measure_type'].lower() == 'ec50'):
                a = 0
                b = 0
                a = math.log10(
                    most_potent['quantitive_efficacy'] / most_potent['quantitive_activity'])
                b = math.log10(
                    i['quantitive_efficacy'] / i['quantitive_activity'])

                return_message = round(a - b, 1)
        except:
            return_message = None
            self.logger.info('lbf_calculate_bias')
        return return_message

    def lbf_calculate_bias_parts(self, i):
        result = None
        try:
            c = 0
            if (i['quantitive_measure_type'].lower() == 'ec50'):
                c = math.log10(
                    i['quantitive_efficacy'] / i['quantitive_activity'])
            result = c
        except:
            self.logger.info('lbf_process_ic50')
            result = None
        return result

    def lbf_process_ic50(self, i):
        return_message = None
        try:
            if (i['quantitive_measure_type'].lower() == 'ic50'):
                return_message = 'Only agonist in main pathway'
        except:
            return_message = None
            self.logger.info('lbf_process_ic50')
        return return_message

    def caclulate_bias_factor_variables(self, a, b):
        '''
        calculations for log bias factor inputs
        '''
        lgb = 0
        try:
            lgb = (a - b)
        except:
            lgb = 0
            self.logger.info('caclulate_bias_factor_variables error')
        return lgb

    def calc_potency_and_transduction(self, biasdata):
        most_potent = dict()
        for i in biasdata:
            if i['order_no'] == 0:
                most_potent = i
        # T_factor -- bias factor
        for i in biasdata:
            if i['order_no'] > 0:
                try:
                    i['potency'] = round(
                        i['quantitive_activity'] - most_potent['quantitive_activity'], 1)
                except:
                    i['potency'] = None
                i['t_factor'] = None
                self.logger.info('t_factor error')

    def count_publications(self, context):
        temp = dict()
        for i in context.items():
            labs = list()
            i[1]['labs'] = 0
            labs.append(i[1]['publication'])
            lab_counter = 1
            for j in context.items():
                if j[1]['publication'] not in labs:
                    if set(i[1]['authors']) & set(j[1]['authors']):
                        lab_counter += 1
                        labs.append(j[1]['publication'])
                        i[1]['labs'] = lab_counter

            temp_obj = 1
            name = str(i[1]['endogenous_ligand']) + \
                '/' + str(i[1]['ligand']) + '/' + str(i[1]['receptor'])
            if name in temp:
                for assays in i[1]['biasdata']:
                    if assays['order_no'] > 0:
                        if assays['log_bias_factor'] != None and assays['log_bias_factor'] != '' or assays['t_factor'] != None and assays['t_factor'] != '':
                            temp_obj = temp[name] + 1

            temp[name] = temp_obj

        for i in context.items():
            temp_obj = 0
            name = str(i[1]['endogenous_ligand']) + \
                '/' + str(i[1]['ligand']) + '/' + str(i[1]['receptor'])
            if name in temp:
                i[1]['article_quantity'] = temp[name]
        self.logger.info('count_publications')

    def fetch_receptor_trunsducers(self, receptor):
        primary = set()
        temp = str()
        temp1 = str()
        secondary = set()
        try:
            gprotein = ProteinGProteinPair.objects.filter(protein=receptor)
            for x in gprotein:
                if x.transduction and x.transduction == 'primary':
                    primary.add(x.g_protein.name)
                elif x.transduction and x.transduction == 'secondary':
                    secondary.add(x.g_protein.name)
            for i in primary:
                temp += str(i.replace(' family', '')) + str(', ')

            for i in secondary:
                temp1 += str(i.replace('family', '')) + str(', ')
            return temp, temp1
        except:
            self.logger.info('receptor not found error')
            return None, None

    def save_data_to_model(self, context, source):
        for i in context['data'].items():
            if len(i[1]['biasdata']) > 1:

                primary, secondary = self.fetch_receptor_trunsducers(
                    i[1]['receptor'])
                experiment_entry = AnalyzedExperiment(publication=i[1]['publication'],
                                                      ligand=i[1]['ligand'],
                                                      external_ligand_ids=i[1]['ligand_links'],
                                                      receptor=i[1]['receptor'],
                                                      source=source,
                                                      endogenous_ligand=i[1]['endogenous_ligand'],
                                                      vendor_quantity=i[1]['vendor_counter'],
                                                      reference_ligand=i[1]['reference_ligand'],
                                                      primary=primary,
                                                      secondary=secondary,
                                                      article_quantity=i[1]['article_quantity'],
                                                      labs_quantity=i[1]['labs'],
                                                      ligand_source_id=i[1]['ligand_source_id'],
                                                      ligand_source_type=i[1]['ligand_source_type']
                                                      )
                experiment_entry.save()
                for ex in i[1]['biasdata']:

                    emax_ligand = ex['emax_reference_ligand']
                    experiment_assay = AnalyzedAssay(experiment=experiment_entry,
                                                     assay_description='predicted_tested_assays',
                                                     family=ex['family'],
                                                     order_no=ex['order_no'],
                                                     signalling_protein=ex['signalling_protein'],
                                                     cell_line=ex['cell_line'],
                                                     assay_type=ex['assay_type'],
                                                     reference_ligand_id=None,
                                                     molecule_1=ex['molecule_1'],
                                                     molecule_2=ex['molecule_2'],
                                                     assay_time_resolved=ex['assay_time_resolved'],
                                                     ligand_function=ex['ligand_function'],
                                                     quantitive_measure_type=ex['quantitive_measure_type'],
                                                     quantitive_activity=ex['quantitive_activity'],
                                                     quantitive_activity_initial=ex['quantitive_activity_initial'],
                                                     quantitive_unit=ex['quantitive_unit'],
                                                     qualitative_activity=ex['qualitative_activity'],
                                                     quantitive_efficacy=ex['quantitive_efficacy'],
                                                     efficacy_measure_type=ex['efficacy_measure_type'],
                                                     efficacy_unit=ex['efficacy_unit'],
                                                     potency=ex['potency'],
                                                     t_coefficient=ex['t_coefficient'],
                                                     t_value=ex['t_coefficient_initial'],
                                                     t_factor=ex['t_factor'],
                                                     log_bias_factor=ex['log_bias_factor'],
                                                     log_bias_factor_a=ex['lbf_a'],
                                                     log_bias_factor_b=ex['lbf_b'],
                                                     log_bias_factor_c=ex['lbf_c'],
                                                     log_bias_factor_d=ex['lbf_d'],
                                                     effector_family=ex['family'],
                                                     measured_biological_process=ex['measured_biological_process'],
                                                     signal_detection_tecnique=ex['signal_detection_tecnique'],
                                                     emax_ligand_reference=emax_ligand
                                                     )
                    experiment_assay.save()
                    # import pdb; pdb.set_trace()

            else:
                self.logger.info('saving error')

    def fetch_experiment(self, publication, ligand, receptor, source):
        '''
        fetch receptor with Protein model
        requires: protein id, source
        '''
        try:
            experiment = AnalyzedExperiment.objects.filter(
                publication=publication, ligand=ligand, receptor=receptor, source=source)
            experiment = experiment.get()
            print('dublicate')
            return True
        except Exception:
            self.logger.info('fetch_experiment error')
            experiment = None
            return False
