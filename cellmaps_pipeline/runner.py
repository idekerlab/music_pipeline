#! /usr/bin/env python

import os
import warnings
import logging
import time
import requests
import networkx as nx
from tqdm import tqdm
from cellmaps_utils import logutils
from cellmaps_utils import constants
from cellmaps_utils.provenance import ProvenanceUtil

from cellmaps_imagedownloader.runner import MultiProcessImageDownloader
from cellmaps_imagedownloader.runner import FakeImageDownloader
from cellmaps_imagedownloader.runner import CellmapsImageDownloader
from cellmaps_imagedownloader.gene import ImageGeneNodeAttributeGenerator
from cellmaps_ppidownloader.runner import CellmapsPPIDownloader
from cellmaps_ppidownloader.gene import APMSGeneNodeAttributeGenerator
from cellmaps_ppi_embedding.runner import Node2VecEmbeddingGenerator
from cellmaps_ppi_embedding.runner import CellMapsPPIEmbedder
from cellmaps_image_embedding.runner import CellmapsImageEmbedder
from cellmaps_image_embedding.runner import FakeEmbeddingGenerator
from cellmaps_image_embedding.runner import DensenetEmbeddingGenerator
from cellmaps_coembedding.runner import MuseCoEmbeddingGenerator
from cellmaps_coembedding.runner import FakeCoEmbeddingGenerator
from cellmaps_coembedding.runner import CellmapsCoEmbedder
from cellmaps_generate_hierarchy.ppi import CosineSimilarityPPIGenerator
from cellmaps_generate_hierarchy.hierarchy import CDAPSHiDeFHierarchyGenerator
from cellmaps_generate_hierarchy.maturehierarchy import HiDeFHierarchyRefiner
from cellmaps_generate_hierarchy.runner import CellmapsGenerateHierarchy
from cellmaps_imagedownloader.proteinatlas import ProteinAtlasReader
from cellmaps_imagedownloader.proteinatlas import ProteinAtlasImageUrlReader
from cellmaps_imagedownloader.proteinatlas import ImageDownloadTupleGenerator
from cellmaps_imagedownloader.proteinatlas import LinkPrefixImageDownloadTupleGenerator

import cellmaps_pipeline
from cellmaps_pipeline.exceptions import CellmapsPipelineError


logger = logging.getLogger(__name__)


class PipelineRunner(object):
    """
    Base command runner
    """

    def __init__(self, outdir):
        """
        Constructor
        """
        self._outdir = os.path.abspath(outdir)

    def _get_image_coembed_tuples(self, fold):
        """

        :param fold:
        :return:
        """
        if fold is None:
            raise CellmapsPipelineError('Fold cannot be None')

        image_coembed_tuples = []
        logger.debug('Fold values: ' + str(fold))
        for fold_val in fold:
            image_embed_dir = os.path.join(self._outdir,
                                           constants.IMAGE_EMBEDDING_STEP_DIR +
                                           str(fold_val))
            co_embed_dir = os.path.join(self._outdir,
                                        constants.COEMBEDDING_STEP_DIR +
                                        str(fold_val))

            image_coembed_tuples.append((fold_val, image_embed_dir,
                                         co_embed_dir))
        logger.debug('Value of image_coembed_tuples: ' +
                     str(image_coembed_tuples))
        return image_coembed_tuples

    def run(self):
        """
        Runs pipeline
        :param cmd:
        :raises NotImplementedError: Always raised cause
                                     subclasses need to implement
        """
        raise NotImplementedError('subclasses need to implement')


class SLURMPipelineRunner(PipelineRunner):
    """
    Generates SLURM batch files and wrapper script to
    run various steps in a SLURM environment
    """

    def __init__(self, outdir=None,
                 cm4ai_apms=None,
                 cm4ai_image=None,
                 samples=None,
                 unique=None,
                 edgelist=None,
                 baitlist=None,
                 model_path=None,
                 proteinatlasxml=None,
                 ppi_cutoffs=None,
                 fake=None,
                 provenance=None,
                 fold=[1],
                 input_data_dict=None,
                 slurm_partition=None,
                 slurm_account=None):
        """

        :param outdir:
        :param samples:
        :param unique:
        :param edgelist:
        :param baitlist:
        :param model_path:
        :param proteinatlasxml:
        :param ppi_cutoffs:
        :param fake:
        :param provenance:
        :param provenance_utils:
        :param fold:
        :param input_data_dict:
        """
        super().__init__(outdir=outdir)
        self._cm4ai_apms = cm4ai_apms
        self._cm4ai_image = cm4ai_image
        self._samples = samples
        self._unique = unique
        self._edgelist = edgelist
        self._baitlist = baitlist
        self._model_path = model_path
        self._fake = fake
        self._provenance = provenance
        self._proteinatlasxml = proteinatlasxml
        self._ppi_cutoffs = ppi_cutoffs
        self._input_data_dict = input_data_dict
        self._slurm_partition = slurm_partition
        self._slurm_account = slurm_account
        self._image_dir = os.path.join(self._outdir,
                                       constants.IMAGE_DOWNLOAD_STEP_DIR)
        self._ppi_dir = os.path.join(self._outdir,
                                     constants.PPI_DOWNLOAD_STEP_DIR)
        self._ppi_embed_dir = os.path.join(self._outdir,
                                           constants.PPI_EMBEDDING_STEP_DIR)

        self._image_coembed_tuples = self._get_image_coembed_tuples(fold)

        self._hierarchy_dir = os.path.join(self._outdir,
                                           constants.HIERARCHY_STEP_DIR)

    def _write_slurm_directives(self, out=None,
                                allocated_time='4:00:00',
                                mem='32G', cpus_per_task='4',
                                job_name='cellmaps_pipeline'):
        """

        :param time:
        :param mem:
        :param cpus_per_task:
        :param job_name:
        :return:
        """
        out.write('#!/bin/bash\n\n')
        out.write('#SBATCH --job-name=' + str(job_name) + '\n')
        out.write('#SBATCH --chdir=' + self._outdir + '\n')

        out.write('#SBATCH --output=%x.%j.out')
        if self._slurm_partition is not None:
            out.write('#SBATCH --partition=' + self._slurm_partition + '\n')
        if self._slurm_account is not None:
            out.write('#SBATCH --account=' + self._slurm_account + '\n')
        out.write('#SBATCH --ntasks=1\n')
        out.write('#SBATCH --cpus-per-task=' + str(cpus_per_task) + '\n')
        out.write('#SBATCH --mem=' + str(mem) + '\n')
        out.write('#SBATCH --time=' + str(allocated_time) + '\n\n')

        out.write('echo $SLURM_JOB_ID\n')
        out.write('echo $HOSTNAME\n')

    def _generate_download_images_command(self):
        """
        Creates command to download images
        :return:
        """
        with open(os.path.join(self._outdir, 'imagedownloadjob.sh'), 'w') as f:
            self._write_slurm_directives(out=f, job_name='imagedownload')
            if self._cm4ai_image != None:
                input_arg = '--cm4ai_table ' + self._cm4ai_image
            elif self._samples != None and self._unique != None:
                input_arg = '--samples ' + self._samples + ' --unique ' + self._unique
            else:
                raise CellmapsPipelineError(
                    'You must provide cm4ai_table parameter or samples and unque parameters.')
            if self._provenance == None:
                raise CellmapsPipelineError(
                    'You must provide provenance parameter')
            f.write('cellmaps_imagedownloadercmd.py ' + self._image_dir +
                    '--provenance ' + self._provenance + ' ' + input_arg + '\n')
            f.write('exit $?\n')

        return 'imagedownloadjob.sh'

    def _generate_download_ppi_command(self):
        """
        Creates command to download ppi
        :return: ppidownloadjob.sh
        """
        with open(os.path.join(self._outdir, 'ppidownloadjob.sh'), 'w') as f:
            self._write_slurm_directives(out=f, job_name='ppidownload')
            if self._cm4ai_image != None:
                input_arg = '--cm4ai_table ' + self._cm4ai_image
            elif self._samples != None and self._unique != None:
                input_arg = '--samples ' + self._samples + ' --unique ' + self._unique
            else:
                raise CellmapsPipelineError(
                    'You must provide cm4ai_table parameter or samples and unque parameters.')
            if self._provenance == None:
                raise CellmapsPipelineError(
                    'You must provide provenance parameter')
            f.write('cellmaps_ppidownloadercmd.py ' + self._ppi_dir +
                    ' --provenance ' + self._provenance + ' ' + input_arg + '\n')
            f.write('exit $?\n')
        return 'ppidownloadjob.sh'

    def _generate_embed_image_command(self, fold=1):
        """
        Creates command to generate image embedding
        :return: imageembedjob.sh
        """
        filename = 'imageembedjob' + str(fold) + '.sh'
        with open(os.path.join(self._outdir, filename), 'w') as f:
            self._write_slurm_directives(out=f, job_name='imageembed' + str(fold))
            fake = '--fake_embedder' if self._fake == True else ""
            f.write('cellmaps_image_embeddingcmd.py ' + self._image_coembed_tuples[fold - 1][1] +
                    ' --fold ' + str(fold) + ' --inputdir ' + self._image_dir + ' ' + fake + ' -vvvv\n')
            f.write('exit $?\n')
        return filename

    def _generate_embed_ppi_command(self):
        """
        Creates command to generate ppi embedding
        :return: ppiembedjob.sh
        """
        with open(os.path.join(self._outdir, 'ppiembedjob.sh'), 'w') as f:
            self._write_slurm_directives(out = f, job_name = 'ppiembed')
            fake="--fake_embedder" if self._fake == True else ""
            f.write('cellmaps_ppi_embeddingcmd.py ' + self._ppi_embed_dir +
                    ' --inputdir ' + self._ppi_dir + ' ' + fake + ' -vvvv\n')
            f.write('exit $?\n')
        return 'ppiembedjob.sh'

    def _generate_coembed_command(self, fold=1):
        """
        Creates command to generate coembedding
        :return: coembedjob.sh
        """
        filename = 'coembeddingjob' + str(fold) + '.sh'
        with open(os.path.join(self._outdir, filename), 'w') as f:
            self._write_slurm_directives(out=f, job_name='coembedding' + str(fold))
            fake = '--fake_embedding' if self._fake == True else ""
            f.write('cellmaps_coembeddingcmd.py ' + self._image_coembed_tuples[fold - 1][2] + ' --ppi_embeddingdir ' + self._ppi_embed_dir +
                    ' --image_embeddingdir ' + self._image_coembed_tuples[fold - 1][1] + ' ' + fake + ' -vvvv\n')
            f.write('exit $?\n')
        return filename

    def _generate_hierarchy_command(self):
        """
        Creates command to generate hierarchy
        :return: hierarchyjob.sh
        """
        with open(os.path.join(self._outdir, 'hierarchyjob.sh'), 'w') as f:
            self._write_slurm_directives(out = f, job_name = 'hierarchy')
            f.write('cellmaps_generate_hierarchycmd.py ' + self._hierarchy_dir + ' --coembedding_dirs ')
            for image_coembed_tuple in self._image_coembed_tuples:
                f.write(image_coembed_tuple[2] + ' ')
            f.write('-vvvv\n')
            f.write('exit $?\n')
        return 'hierarchyjob.sh'


    def run(self):
        """
        Runs pipeline
        :param cmd:
        :raises NotImplementedError: Always raised cause
                                     subclasses need to implement
        """
        slurmjobfile=os.path.join(self._outdir, 'slurm_cellmaps_job.sh')
        with open(slurmjobfile, 'w') as f:
            f.write('#! /bin/bash\n\n')
            f.write('# image download no dependencies\n')
            f.write('image_download_job=$(sbatch ' +
                    self._generate_download_images_command() + ')\n\n')

            f.write('# ppi download no dependencies\n')
            f.write('ppi_download_job=$(sbatch ' +
                    self._generate_download_ppi_command() + ')\n\n')

            f.write('# ppi embed\n')
            f.write('ppi_embed_job=$(sbatch --dependency=afterok:$ppi_download_job ' +
                    self._generate_embed_ppi_command() + ')\n\n')

            embed_job_names = ['$ppi_embed_job']
            for image_coembed_tuple in self._image_coembed_tuples:
                # [0] = fold value
                # [1] = image embedding dir
                # [2] = outdir
                f.write('# image embed\n')
                f.write('image_embed_job' + str(image_coembed_tuple[0]) + '=$(sbatch --dependency=afterok:$image_download_job ' +
                        self._generate_embed_image_command(fold=image_coembed_tuple[0]) + ')\n\n')
                f.write(
                    '# fold' + str(image_coembed_tuple[0]) + ' co-embedding\n')
                embed_job_name='f' + str(image_coembed_tuple) + '_coembed_job'
                f.write(embed_job_name + '=$(sbatch --dependency=afterok:$image_embed_job' + str(image_coembed_tuple[0]) + ' ' +
                        self._generate_coembed_command(fold=image_coembed_tuple[0]))
                embed_job_names.append('$' + embed_job_name)
            dependency_str = ':'.join(embed_job_names)
            f.write('# hierarchy\n')
            f.write('hierarchy_job=$(sbatch --dependency=afterok:' + dependency_str + ' ' + self._generate_hierarchy_command() + ')\n\n')

        # Todo need to


class ProgrammaticPipelineRunner(PipelineRunner):
    """
    Runs pipeline programmatically in a serial fashion

    """

    def __init__(self, outdir = None,
                 samples=None,
                 unique=None,
                 edgelist=None,
                 baitlist=None,
                 model_path=None,
                 proteinatlasxml=None,
                 ppi_cutoffs=None,
                 fake=None,
                 provenance=None,
                 provenance_utils=ProvenanceUtil(),
                 fold=[1],
                 input_data_dict=None):
        """
        Constructor
        """
        super().__init__(outdir=outdir)
        self._samples = samples
        self._unique = unique
        self._edgelist = edgelist
        self._baitlist = baitlist
        self._model_path = model_path
        self._fake = fake
        self._provenance = provenance
        self._provenance_utils = provenance_utils
        self._proteinatlasxml = proteinatlasxml
        self._ppi_cutoffs = ppi_cutoffs
        self._input_data_dict = input_data_dict
        self._image_dir = os.path.join(self._outdir,
                                       constants.IMAGE_DOWNLOAD_STEP_DIR)
        self._ppi_dir = os.path.join(self._outdir,
                                     constants.PPI_DOWNLOAD_STEP_DIR)
        self._ppi_embed_dir = os.path.join(self._outdir,
                                           constants.PPI_EMBEDDING_STEP_DIR)

        self._image_coembed_tuples = self._get_image_coembed_tuples(fold)

        self._hierarchy_dir = os.path.join(self._outdir,
                                           constants.HIERARCHY_STEP_DIR)

    def run(self):
        """
        Runs pipeline programmatically in serial steps. This would
        be the same as running the steps in a notebook

        :raises CellmapsPipelineError: if command returns non zero value

        """
        if self._download_images() != 0:
            raise CellmapsPipelineError('Image download failed')

        if self._download_ppi() != 0:
            raise CellmapsPipelineError('PPI download failed')

        if self._embed_ppi() != 0:
            raise CellmapsPipelineError('PPI embed failed')

        if self._embed_image() != 0:
            raise CellmapsPipelineError('Image embed failed')

        if self._coembed() != 0:
            raise CellmapsPipelineError('Coembed failed')

        if self._hierarchy() != 0:
            raise CellmapsPipelineError('Hierarchy failed')

        return 0

    def _hierarchy(self):
        """

        :return:
        """
        if os.path.isdir(self._hierarchy_dir):
            warnings.warn(
                'Found hierarchy dir, assuming we are good. skipping')
            return 0

        coembed_dirs = []
        for image_coembed_tuple in self._image_coembed_tuples:
            coembed_dirs.append(image_coembed_tuple[2])

        logger.debug('Coembedding directories: ' + str(coembed_dirs))

        ppigen = CosineSimilarityPPIGenerator(embeddingdirs=coembed_dirs,
                                              cutoffs=self._ppi_cutoffs)

        refiner = HiDeFHierarchyRefiner(
            provenance_utils=self._provenance_utils)

        hiergen = CDAPSHiDeFHierarchyGenerator(refiner=refiner,
                                               provenance_utils=self._provenance_utils)
        return CellmapsGenerateHierarchy(outdir=self._hierarchy_dir,
                                         inputdirs=coembed_dirs,
                                         ppigen=ppigen,
                                         hiergen=hiergen,
                                         input_data_dict=self._input_data_dict,
                                         provenance_utils=self._provenance_utils).run()

    def _coembed(self):
        """

        :return:
        """
        for image_coembed_tuple in self._image_coembed_tuples:
            if os.path.isdir(image_coembed_tuple[2]):
                warnings.warn('Found coembedding dir' +
                              str(image_coembed_tuple[2]) +
                              ', assuming we are good. skipping')
                continue
            if self._fake:
                gen = FakeCoEmbeddingGenerator(ppi_embeddingdir=self._ppi_embed_dir,
                                               image_embeddingdir=image_coembed_tuple[1])
            else:
                gen = MuseCoEmbeddingGenerator(outdir=image_coembed_tuple[2],
                                               ppi_embeddingdir=self._ppi_embed_dir,
                                               image_embeddingdir=image_coembed_tuple[1])
            retval = CellmapsCoEmbedder(outdir=image_coembed_tuple[2],
                                        inputdirs=[image_coembed_tuple[1],
                                                   self._ppi_embed_dir],
                                        embedding_generator=gen,
                                        input_data_dict=self._input_data_dict).run()
            if retval != 0:
                logger.error('Coembedding ' + image_coembed_tuple[2] +
                             'using ' + image_coembed_tuple[1] +
                             ' had non zero exit code of: ' +
                             str(retval))
                return retval
        return 0

    def _embed_image(self):
        """

        :return:
        """
        for image_coembed_tuple in self._image_coembed_tuples:
            if os.path.isdir(image_coembed_tuple[1]):
                warnings.warn('Found image_embedding dir' +
                              str(image_coembed_tuple[1]) +
                              ', assuming we are good. skipping')
                continue
            if self._fake is True:
                gen = FakeEmbeddingGenerator(self._image_dir)
            else:
                gen = DensenetEmbeddingGenerator(self._image_dir,
                                                 outdir=image_coembed_tuple[1],
                                                 model_path=self._model_path,
                                                 fold=int(image_coembed_tuple[0]))
            retval = CellmapsImageEmbedder(outdir=image_coembed_tuple[1],
                                           inputdir=self._image_dir,
                                           embedding_generator=gen,
                                           input_data_dict=self._input_data_dict).run()
            if retval != 0:
                logger.error('image embedding ' + image_coembed_tuple[1] +
                             'using fold' + str(image_coembed_tuple[0] +
                                                ' had non zero exit code of: ' +
                                                str(retval)))
                return retval
        return 0

    def _embed_ppi(self):
        """

        :return:
        """
        if os.path.isdir(self._ppi_embed_dir):
            warnings.warn(
                'Found ppi embedding dir, assuming we are good. skipping')
            return 0
        gen = Node2VecEmbeddingGenerator(
            nx_network=nx.read_edgelist(CellMapsPPIEmbedder.get_apms_edgelist_file(self._ppi_dir),
                                        delimiter='\t'))

        return CellMapsPPIEmbedder(outdir=self._ppi_embed_dir,
                                   embedding_generator=gen,
                                   inputdir=self._ppi_dir,
                                   input_data_dict=self._input_data_dict).run()

    def _download_ppi(self):
        """

        :return:
        """
        if os.path.isdir(self._ppi_dir):
            warnings.warn('Found ppi dir, assuming we are good. skipping')
            return 0
        apmsgen = APMSGeneNodeAttributeGenerator(
            apms_edgelist=APMSGeneNodeAttributeGenerator.get_apms_edgelist_from_tsvfile(
                self._edgelist),
            apms_baitlist=APMSGeneNodeAttributeGenerator.get_apms_baitlist_from_tsvfile(self._baitlist))

        return CellmapsPPIDownloader(outdir=self._ppi_dir,
                                     apmsgen=apmsgen,
                                     input_data_dict=self._input_data_dict,
                                     provenance=self._provenance).run()

    def _download_images(self):
        """
        Downloads Images using
        :py:class:`~cellmaps_imagedownloader.runner.CellmapsImageDownloader`

        :return: exit code of :py:meth:`~cellmaps_imagedownloader.runner.CellmapsImageDownloader.run`
        :rtype: int
        """
        if os.path.isdir(self._image_dir):
            warnings.warn('Found image dir, assuming we are good. skipping')
            return 0
        logger.info('Downloading images')

        imagegen = ImageGeneNodeAttributeGenerator(
            unique_list=ImageGeneNodeAttributeGenerator.get_unique_list_from_csvfile(
                self._unique),
            samples_list=ImageGeneNodeAttributeGenerator.get_samples_from_csvfile(self._samples))

        if 'linkprefix' in imagegen.get_samples_list()[0]:
            logger.debug(
                'linkprefix in samples using LinkPrefixImageDownloadTupleGenerator')
            imageurlgen = LinkPrefixImageDownloadTupleGenerator(
                samples_list=imagegen.get_samples_list())
        else:
            proteinatlas_reader = ProteinAtlasReader(
                self._image_dir, proteinatlas=self._proteinatlasxml)
            proteinatlas_urlreader = ProteinAtlasImageUrlReader(
                reader=proteinatlas_reader)
            imageurlgen = ImageDownloadTupleGenerator(reader=proteinatlas_urlreader,
                                                      samples_list=imagegen.get_samples_list())

        if self._fake is True:
            warnings.warn('FAKE IMAGES ARE BEING DOWNLOADED!!!!!')
            dloader = FakeImageDownloader()
        else:
            dloader = MultiProcessImageDownloader()
        # Todo: input_data_dict should NOT be required to run this
        #       https://github.com/idekerlab/cellmaps_imagedownloader/issues/2
        return CellmapsImageDownloader(outdir=self._image_dir,
                                       imagedownloader=dloader,
                                       imagegen=imagegen,
                                       imageurlgen=imageurlgen,
                                       provenance=self._provenance,
                                       skip_failed=True,
                                       input_data_dict=self._input_data_dict).run()


class CellmapsPipeline(object):
    """
    Class to run algorithm
    """

    def __init__(self, outdir=None,
                 runner=None,
                 input_data_dict=None):
        """
        Constructor

        :param exitcode: value to return via :py:meth:`.CellmapsPipeline.run` method
        :type int:
        """
        if outdir is None:
            raise CellmapsPipelineError('outdir is None')

        self._outdir = os.path.abspath(outdir)
        self._start_time = int(time.time())
        self._runner = runner
        self._input_data_dict = input_data_dict
        logger.debug('In constructor')

    def run(self):
        """
        Runs CM4AI Pipeline


        :return:
        """
        logger.debug('In run method')
        if self._outdir is None:
            raise CellmapsPipelineError('outdir must be set')

        if not os.path.isdir(self._outdir):
            os.makedirs(self._outdir, mode=0o755)

        logutils.write_task_start_json(outdir=self._outdir,
                                       start_time=self._start_time,
                                       data={
                                           'commandlineargs': self._input_data_dict},
                                       version=cellmaps_pipeline.__version__)

        exit_status = 99
        try:
            exit_status = self._runner.run()
        finally:
            logutils.write_task_finish_json(outdir=self._outdir,
                                            start_time=self._start_time,
                                            status=exit_status)
        return exit_status
