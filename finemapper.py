import numpy as np; np.set_printoptions(precision=4, linewidth=200)
import pandas as pd; pd.set_option('display.width', 200)
import os
import time
import scipy.stats as stats
import logging
import gzip
from tqdm import tqdm
import tempfile
import glob
import subprocess
from importlib import reload


class Fine_Mapping(object):
    def __init__(self, genotypes_file, sumstats_file, n, chr_num, ldstore_exe, 
                    sample_file=None, incl_samples=None, cache_dir=None, n_threads=None
):
    
        #read sumstats and filter to target chromosome only
        logging.info('Loading sumstats file...')        
        if sumstats_file.endswith('.parquet'):
            df_sumstats = pd.read_parquet(sumstats_file)
        else: 
            df_sumstats = pd.read_csv(sumstats_file, delim_whitespace=True)
        if not np.any(df_sumstats['CHR'] == chr_num):
            raise IOError('sumstats file does not include any SNPs in chromosome %s'%(chr_num))
        if np.any(df_sumstats['CHR'] != chr_num):
            df_sumstats = df_sumstats.query('CHR==%s'%(chr_num)).copy()
        df_sumstats.index = df_sumstats['SNP'] + '.' + df_sumstats['A1'] + '.' + df_sumstats['A2']
        if 'P' not in df_sumstats.columns:
            df_sumstats['P'] = stats.chi2(1).sf(df_sumstats['Z']**2)
        logging.info('Loaded sumstats for %d SNPs'%(df_sumstats.shape[0]))
        
        #save class members
        if genotypes_file.endswith('.bgen'):
            if sample_file is None:
                raise IOError('sample-file must be provided with a bgen file')
        self.genotypes_file = genotypes_file
        self.n = n
        self.sample_file = sample_file
        self.df_sumstats = df_sumstats
        self.incl_samples = incl_samples
        self.ldstore_exe = ldstore_exe
        self.cache_dir = cache_dir
        self.n_threads = n_threads
        self.chr = chr_num
        
        
            
    def set_locus(self, locus_start, locus_end, read_ld_matrix=False, verbose=False):
    
        #update self.df_sumstats_locus
        self.df_sumstats_locus = self.df_sumstats.query('%d <= BP <= %d'%(locus_start, locus_end))
        if self.df_sumstats_locus.shape[0] == 0:
            raise ValueError('No SNPs found in sumstats file in the BP range %d-%d'%(locus_start, locus_end))
                
        #define file names
        if self.cache_dir is None:
            ld_dir = tempfile.mkdtemp()
        else:
            ld_dir = self.cache_dir
        if self.incl_samples is None:
            ldstore_basename = os.path.join(ld_dir, '%s.%d.%d.%d'%(os.path.basename(self.genotypes_file), self.chr, locus_start, locus_end))
        else:
            ldstore_basename = os.path.join(ld_dir, '%s.%s.%d.%d.%d'%(os.path.basename(self.genotypes_file), os.path.basename(self.incl_samples), self.chr, locus_start, locus_end))
        bcor_file = ldstore_basename+'.bcor'
        meta_file = ldstore_basename+'.meta.txt'
        incl_variants_file = ldstore_basename+'.incl'
        ld_matrix_file = ldstore_basename+'.ld'
        self.ld_matrix_file = ldstore_basename+'.ld'
        ld_matrix_file = self.ld_matrix_file

        #check if we already have the required file in the cache
        found_cached_ld_file = False
        rewrite_ld = False
        if self.cache_dir is not None:
            if self.incl_samples is None:
                fname_pattern = '%s.%d'%(os.path.basename(self.genotypes_file), self.chr)
            else:
                fname_pattern = '%s.%s.%d'%(os.path.basename(self.genotypes_file), os.path.basename(self.incl_samples), self.chr)
            ld_files = glob.glob(os.path.join(self.cache_dir, fname_pattern+'*.ld'))
            for ld_file in ld_files:
                ld_basename = os.path.basename(ld_file)                
                bp1 = int(ld_basename.split('.')[-3])
                bp2 = int(ld_basename.split('.')[-2])
                assert bp1 < bp2
                if (bp1 > locus_start) or (bp2 < locus_end): continue
                
                #Make sure that the LD matrix contains the SNPs that we need
                incl_file_cached = ld_file[:-3]+'.incl'
                meta_file_cached = ld_file[:-3]+'.meta.txt'
                if os.path.exists(incl_file_cached):
                    df_meta = pd.read_table(incl_file_cached, sep=' ')
                elif os.path.exists(meta_file_cached):
                    df_meta = pd.read_table(meta_file_cached, sep=' ')
                else:
                    continue
                df_meta.index = df_meta['RSID'] + '.' + df_meta['A_allele'] + '.' + df_meta['B_allele']
                if not np.all(self.df_sumstats_locus.index.isin(df_meta.index)):
                    continue
            
                #if we got here than we found a suitable LD file
                ld_matrix_file = ld_file
                logging.info('Found an existing LD file containing all SNPs with sumstats in chromosome %d BP %d-%d'%(self.chr, locus_start, locus_end))
                found_cached_ld_file = True
                cache_snps = df_meta.index
                rewrite_ld = (not read_ld_matrix and len(cache_snps) > self.df_sumstats_locus.shape[0])
                break
        
        #run LDstore if we didnt find a suitable file in the cache
        if not found_cached_ld_file:
            logging.info('Computing LD matrix for chromosome %d BP %d-%d'%(self.chr, locus_start, locus_end))
            t0 = time.time()
            ldstore_cmd = [self.ldstore_exe]
            ldstore_cmd += ['--bcor', bcor_file]            
            ldstore_cmd += ['--incl-range', '%d-%d'%(locus_start, locus_end)]
            if self.n_threads is not None:
                ldstore_cmd += ['--n-threads', str(self.n_threads)]
            if os.path.exists(self.genotypes_file + '.bed'):
                ldstore_cmd += ['--bplink', self.genotypes_file]
            elif self.genotypes_file.endswith('.bgen'):
                ldstore_cmd += ['--bgen', self.genotypes_file]
            else:
                raise IOError('Neither a plink nor a bgen file was found')
            if self.incl_samples is not None:
                ldstore_cmd += ['--samples', self.sample_file]        
                ldstore_cmd += ['--incl-samples', self.incl_samples]        
            run_executable(ldstore_cmd, 'LDStore', measure_time=True, show_output=verbose, show_command=verbose)
                
            #run LDStore merge if needed
            bcor_files = glob.glob(bcor_file + '_*')
            num_bcor_files = len(bcor_files)
            if num_bcor_files == 0:
                raise IOError('No bcor files found')
            elif num_bcor_files == 1:
                os.rename(bcor_files[0], bcor_file)
            else:
                ldstore_merge_cmd = [self.ldstore_exe]
                ldstore_merge_cmd += ['--bcor', bcor_file]
                ldstore_merge_cmd += ['--merge', str(num_bcor_files)]
                run_executable(ldstore_merge_cmd, 'LDStore merge', measure_time=False, show_output=verbose, show_command=verbose)                                
            
            #run LDStore meta        
            ldstore_meta_cmd = [self.ldstore_exe]
            ldstore_meta_cmd += ['--bcor', bcor_file]
            ldstore_meta_cmd += ['--meta', meta_file]
            run_executable(ldstore_meta_cmd, 'LDStore meta', measure_time=False, show_output=verbose, show_command=verbose)
        
            #open meta_file
            df_ldstore_meta = pd.read_table(meta_file, delim_whitespace=True)
            df_ldstore_meta.index = df_ldstore_meta['RSID'] + '.' + df_ldstore_meta['A_allele'] + '.' + df_ldstore_meta['B_allele']
            if not np.all(self.df_sumstats_locus.index.isin(df_ldstore_meta.index)):
                raise IOError('Not all variants exist in LDStore output')
                
            #create incl-variants file if needed
            if df_ldstore_meta.shape[0] == self.df_sumstats_locus.shape[0]:
                use_incl_file = False
            else:
                assert np.all(self.df_sumstats_locus.index.isin(df_ldstore_meta.index))
                df_ldstore_meta = df_ldstore_meta.loc[self.df_sumstats_locus.index, ['RSID', 'position', 'chromosome', 'A_allele', 'B_allele']]
                df_ldstore_meta.to_csv(incl_variants_file, index=False, header=True, sep=' ')
                use_incl_file = True
            
            #extract LD matrix to a text file (finally!!!)        
            logging.info('Extracting LD matrix to a text file')
            t0 = time.time()            
            ldstore_ld_cmd = [self.ldstore_exe]
            ldstore_ld_cmd += ['--bcor', bcor_file]
            ldstore_ld_cmd += ['--matrix', self.ld_matrix_file]
            if use_incl_file:
                ldstore_ld_cmd += ['--incl-variants', incl_variants_file]
            run_executable(ldstore_ld_cmd, 'LDStore LD extraction', measure_time=True, show_output=verbose, show_command=verbose)        
            
            
        #read LD matrix
        if read_ld_matrix or rewrite_ld:
            df_R = pd.read_table(ld_matrix_file, delim_whitespace=True, index_col=None, header=None)
            if df_R.shape[0] != df_R.shape[1]:
                raise IOError('LDStore LD matrix has inconsistent rows/columns')
            if not found_cached_ld_file:
                df_R.index = self.df_sumstats_locus.index
                df_R.columns = self.df_sumstats_locus.index
            else:
                df_R.index = cache_snps
                df_R.columns = cache_snps                
                assert np.all(self.df_sumstats_locus.index.isin(df_R.index))                
                if df_R.shape[0] > self.df_sumstats_locus.shape[0]:
                    df_R = df_R.loc[self.df_sumstats_locus.index, self.df_sumstats_locus.index]
            
            #rewrite LD matrix if needed
            if rewrite_ld:
                df_R.to_csv(self.ld_matrix_file, sep=' ', index=False, header=False, float_format='%0.8f')
            
            #save df_R to a class member if needed
            if read_ld_matrix:
                self.df_R = df_R
            


    def finemap(self):
        raise NotImplementedError()
        
        
        
    def estimate_h2_hess(self, prop_keep=0.005, R_cutoff=0.99, pvalue_bound=None):
        '''
            prop_keep:  Proprtion of SNPs to use in the estimation (only the ones with the smallest p-values)
            R_cutoff: Exclude one of each pair of SNPs with with magnitude of correlation greater than this value
            pvalue_bound: An upper bound on the p-value cutoff (i.e., SNPs with P greater than this cutoff will never be used in the estimation)
        '''
        
        #keep only potential causal SNPs        
        pvalue_cutoff = self.df_sumstats['P'].quantile(prop_keep)
        if pvalue_cutoff==0:
            pvalue_cutoff = np.min(self.df_sumstats['P'].loc[lambda p:p>0])
        if pvalue_bound is not None and pvalue_cutoff>pvalue_bound:
            pvalue_cutoff = pvalue_bound
        is_potential_csnp = self.df_sumstats['P']<pvalue_cutoff
        if np.any(is_potential_csnp):
            R_pot_csnp = self.df_R.loc[is_potential_csnp, is_potential_csnp].values
        else:
            return 0

        #take a maximally independent subset
        np.fill_diagonal(R_pot_csnp,0)
        import networkx as nx
        G = nx.from_numpy_matrix(np.abs(R_pot_csnp)>R_cutoff)
        np.fill_diagonal(R_pot_csnp,1)
        inds = np.sort(nx.maximal_independent_set(G))
        
        #estimate h2 using HESS
        R_subset = R_pot_csnp[np.ix_(inds, inds)]
        alpha_subset = self.df_sumstats.loc[is_potential_csnp, 'Z'].iloc[inds].values / np.sqrt(self.n)
        h2_hess = alpha_subset.dot(np.linalg.solve(R_subset, alpha_subset)) - R_subset.shape[0]/self.n
        
        return h2_hess
        
        
    def estimate_h2_hess_wrapper(self, prop_keep=0.005, R_cutoff=0.99, min_h2=1e-4, num_samples=100):
        '''
            prop_keep:  Proprtion of SNPs to use in the estimation (only the ones with the smallest p-values)
            R_cutoff: Exclude one of each pair of SNPs with with magnitude of correlation greater than this value
            min_h2: Exclude SNPs that tag less than this amount of heritability
            num_samples: Number of random samples of indepdendent SNPs to draw        
        '''

        if min_h2 is None:
            pvalue_bound = None
        else:
            pvalue_bound = stats.chi2(1).sf(min_h2 * self.n)
        
        h2_hess_list = [self.estimate_h2_hess(prop_keep=prop_keep, R_cutoff=R_cutoff, pvalue_bound=pvalue_bound) \
                        for try_num in range(num_samples)]
        h2_hess = np.mean(h2_hess_list)
        return h2_hess        
            
    
    
    
    
    
class SUSIE_Wrapper(Fine_Mapping):

    def __init__(self, genotypes_file, sumstats_file, n, chr_num, ldstore_exe, sample_file=None, incl_samples=None, cache_dir=None, n_threads=None):

        super(SUSIE_Wrapper, self).__init__(genotypes_file, sumstats_file, n, chr_num, ldstore_exe=ldstore_exe, sample_file=sample_file, incl_samples=incl_samples,
                                           cache_dir=cache_dir, n_threads=n_threads)
                       
        #load SuSiE R package
        import rpy2
        import rpy2.robjects.numpy2ri as numpy2ri
        import rpy2.robjects as ro
        ro.conversion.py2ri = numpy2ri
        numpy2ri.activate()
        from rpy2.robjects.packages import importr
        self.susieR = importr('susieR')
        self.R_null = ro.rinterface.NULL
        self.RNULLType = rpy2.rinterface.RNULLType
               
        
    def finemap(self, locus_start, locus_end, num_causal_snps, use_prior_causal_prob=True, prior_var=None, residual_var=None, hess=False, verbose=False):
    
        #set locus
        self.set_locus(locus_start, locus_end, read_ld_matrix=True, verbose=verbose)
        
        #define prior causal probabilities
        if use_prior_causal_prob:
            if 'SNPVAR' not in self.df_sumstats_locus.columns:
                raise ValueError('SNPVAR column not found in sumstats file')
            prior_weights = self.df_sumstats_locus['SNPVAR'].copy().values
            prior_weights /= prior_weights.sum()
            assert np.isclose(prior_weights.sum(), 1)
            
            
        #Use HESS to estimate causal effect sizes
        if hess:
            if prior_var is not None:
                raise ValueError('cannot specify both hess and a custom prior_var')
            prior_var = self.estimate_h2_hess() / num_causal_snps
            if prior_var <= 0:
                raise ValueError('HESS estimates that the locus causally explains zero heritability')
            logging.info('HESS estimated causal effect size variance: %0.4e'%(prior_var))
    
        #rpy2 bug fix
        import rpy2.robjects.numpy2ri as numpy2ri
        reload(numpy2ri)
        numpy2ri.activate()
        
        #run SuSiE
        t0 = time.time()
        m = self.df_sumstats_locus.shape[0]        
        logging.info('Starting %s SuSiE fine-mapping for chromosome %d BP %d-%d'%(
            ('functionally-informed' if use_prior_causal_prob else 'non-functionally informed'),
            self.chr,
            locus_start,
            locus_end))
            
        # susie_obj = self.susieR.susie_z(
                # z=self.df_sumstats_locus['Z'].values.reshape((m,1)),
                # R=self.df_R.values,
                # n=self.n,
                # L=num_causal_snps,
                # prior_variance=(0.0001 if (prior_var is None) else prior_var),
                # estimate_prior_variance=(prior_var is None),
                # residual_variance=(self.R_null if (residual_var is None) else residual_var),
                # estimate_residual_variance=(residual_var is None),
                # verbose=verbose,
                # prior_weights=(prior_weights.reshape((m,1)) if use_prior_causal_prob else self.R_null)
            # )
        susie_obj = self.susieR.susie_bhat(
                bhat=self.df_sumstats_locus['Z'].values.reshape((m,1)),
                shat=np.ones((m,1)),
                R=self.df_R.values,
                n=self.n,
                L=num_causal_snps,
                scaled_prior_variance=(0.0001 if (prior_var is None) else prior_var),
                estimate_prior_variance=(prior_var is None),
                residual_variance=(self.R_null if (residual_var is None) else residual_var),
                estimate_residual_variance=(residual_var is None),
                verbose=verbose,
                prior_weights=(prior_weights.reshape((m,1)) if use_prior_causal_prob else self.R_null)
            )            
        susie_time = time.time()-t0
        if verbose:
            logging.info('Done in %0.2f seconds'%(susie_time))
        
        #extract pip and beta_mean
        pip = np.array(self.susieR.susie_get_pip(susie_obj))
        beta_mean = np.array(self.susieR.coef_susie(susie_obj)[1:])
        assert np.allclose(beta_mean, np.sum(np.array(susie_obj.rx2('mu')) * np.array(susie_obj.rx2('alpha')), axis=0) / np.array(susie_obj.rx2('X_column_scale_factors')))

        #compute the posterior mean of beta^2
        s_alpha = np.array(susie_obj.rx2('alpha'))
        s_mu = np.array(susie_obj.rx2('mu'))
        s_mu2 = np.array(susie_obj.rx2('mu2'))
        s_X_column_scale_factors = np.array(susie_obj.rx2('X_column_scale_factors'))
        beta_var = np.sum(s_alpha*s_mu2 - (s_alpha*s_mu)**2, axis=0) / (s_X_column_scale_factors**2)
        assert np.all(beta_var>=0)
        
        #create output df
        df_susie = self.df_sumstats_locus.copy()
        df_susie['PIP'] = pip
        df_susie['BETA_MEAN'] = beta_mean
        df_susie['BETA_SD'] = np.sqrt(beta_var)
        
        #mark causal sets
        self.susie_dict = {key:np.array(susie_obj.rx2(key)) for key in list(susie_obj.names)}
        df_susie['CREDIBLE_SET'] = 0
        susie_sets = self.susie_dict['sets'][0]
        if type(susie_sets) != self.RNULLType:
            for set_i, susie_set in enumerate(susie_sets):
                is_in_set = np.zeros(df_susie.shape[0], dtype=np.bool)
                is_in_set[np.array(susie_set)-1] = True
                is_in_set[df_susie['CREDIBLE_SET']>0] = False
                df_susie.loc[is_in_set, 'CREDIBLE_SET'] = set_i+1
        
        return df_susie
        
    
    
    
    


def run_executable(cmd, description, good_returncode=0, measure_time=True, check_errors=True, show_output=False, show_command=False):
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    logging.info('Running %s...'%(description))
    if show_command:
        logging.info('Command: %s'%(' '.join(cmd)))
    t0 = time.time()
    stdout = []
    if show_output:
        for line in proc.stdout:
            if len(line.strip()) > 0:
                line_str = line.strip().decode("utf-8")
                stdout.append(line_str)
                print(line_str)
        print()
        stdout = '\n'.join(stdout)
        _, stderr = proc.communicate()
    else:
        stdout, stderr = proc.communicate()
        if stdout is not None:
            stdout = stdout.decode('ascii')
            if len(stdout)==0: stdout=None
    if stderr is not None:
        stderr = stderr.decode('ascii')
        if len(stderr)==0: stderr=None        
        
    #if (stderr is not None or proc.returncode != good_returncode):
    if proc.returncode != good_returncode:
        if stderr is not None:            
            logging.error('stderr:\n%s'%(stderr))
        if stdout is not None and not show_output:            
            logging.error('stdout:\n%s'%(stdout))
        raise RuntimeError('%s error'%(description))
    if measure_time:
        logging.info('done in %0.2f seconds'%(time.time() - t0))
        
    if check_errors and stdout is not None:        
        for l in stdout.split('\n'):
            if 'error' in l.lower():
                logging.error(l)
                raise RuntimeError('%s reported an error'%(description))
    if check_errors and stderr is not None:
            for l in stderr.split('\n'):
                if 'error' in l.lower():
                    logging.error(l)
                    raise RuntimeError('%s reported an error'%(description))
        
    return stdout, stderr
    