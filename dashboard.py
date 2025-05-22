import streamlit as st
import pandas as pd
import xmlrpc.client
from datetime import datetime, timedelta, date
import logging
import altair as alt
import os

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    filename='dashboard.log',
    force=True
)
logger = logging.getLogger(__name__)

# Initialize session state
if 'odoo_uid' not in st.session_state:
    st.session_state.odoo_uid = None
if 'odoo_models' not in st.session_state:
    st.session_state.odoo_models = None
if 'model_fields_cache' not in st.session_state:
    st.session_state.model_fields_cache = {}
if 'last_error' not in st.session_state:
    st.session_state.last_error = None
if 'employee_data' not in st.session_state:
    st.session_state.employee_data = None

# Permanent Odoo connection credentials
ODOO_URL = "https://prezlab.odoo.com/"
ODOO_DB = "odoo-ps-psae-prezlab-main-10779811"
ODOO_USERNAME = "sanad.zaqtan@prezlab.com"
ODOO_PASSWORD = "ODOOprezlab123"

# Set session state values with permanent credentials
st.session_state.odoo_url = ODOO_URL
st.session_state.odoo_db = ODOO_DB
st.session_state.odoo_username = ODOO_USERNAME
st.session_state.odoo_password = ODOO_PASSWORD

# Load employee data at startup
def load_employee_data():
    try:
        csv_path = "uhd_data.csv"
        if os.path.exists(csv_path):
            logger.info(f"Loading employee data from CSV: {csv_path}")
            df = pd.read_csv(csv_path)
            
            # Verify required columns exist
            required_columns = ["Employee Name", "Manager", "Work Email", "Microsoft ID"]
            missing_columns = [col for col in required_columns if col not in df.columns]
            if missing_columns:
                logger.error(f"Required columns missing from CSV: {missing_columns}")
                return None
            
            logger.info(f"Successfully loaded employee data with {len(df)} rows")
            return df
        else:
            logger.error(f"Employee data file not found: {csv_path}")
            return None
    except Exception as e:
        logger.error(f"Error loading employee data: {e}")
        return None

def authenticate_odoo(url, db, username, password):
    """Authenticate with Odoo and return uid and models"""
    try:
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
        uid = common.authenticate(db, username, password, {})
        
        if not uid:
            st.error("Odoo authentication failed - invalid credentials")
            logger.error("Odoo authentication failed - invalid credentials")
            return None, None
            
        models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
        logger.info(f"Successfully connected to Odoo (UID: {uid})")
        return uid, models
    except Exception as e:
        logger.error(f"Odoo connection error: {e}")
        st.error(f"Odoo connection error: {e}")
        st.session_state.last_error = str(e)
        return None, None

def get_model_fields(models, uid, odoo_db, odoo_password, model_name):
    """Get fields for a specific model, with caching"""
    # Check if we have cached fields for this model
    if model_name in st.session_state.model_fields_cache:
        return st.session_state.model_fields_cache[model_name]
    
    try:
        fields = models.execute_kw(
            odoo_db, uid, odoo_password,
            model_name, 'fields_get',
            [],
            {'attributes': ['string', 'type', 'relation']}
        )
        # Cache the result
        st.session_state.model_fields_cache[model_name] = fields
        return fields
    except Exception as e:
        logger.error(f"Error getting fields for model {model_name}: {e}")
        st.session_state.last_error = str(e)
        return {}

def get_planning_slots(models, uid, odoo_db, odoo_password, start_date, end_date=None, shift_status_filter=None):
    """
    Get planning slots for a date range, with a focus on finding all slots 
    that overlap with the given date range. Optionally filter by x_studio_shift_status.
    """
    try:
        # Get the fields for planning.slot model
        fields_info = get_model_fields(models, uid, odoo_db, odoo_password, 'planning.slot')
        available_fields = list(fields_info.keys())
        
        # Handle single date or date range
        if end_date is None:
            end_date = start_date
        
        # Prepare the date strings
        start_date_str = start_date.strftime("%Y-%m-%d")
        end_date_str = end_date.strftime("%Y-%m-%d")
        next_date_str = (end_date + timedelta(days=1)).strftime("%Y-%m-%d")
        
        # Create different domain variations to catch various date formats and conditions
        base_domains = [
            # Standard format with start/end datetime - get all slots in the date range
            [
                '|',
                # Slots that start in our date range
                '&', ('start_datetime', '>=', f"{start_date_str} 00:00:00"), ('start_datetime', '<', f"{next_date_str} 00:00:00"),
                # Slots that end in our date range or overlap with it
                '|',
                '&', ('end_datetime', '>=', f"{start_date_str} 00:00:00"), ('end_datetime', '<', f"{next_date_str} 00:00:00"),
                '&', ('start_datetime', '<', f"{start_date_str} 00:00:00"), ('end_datetime', '>=', f"{next_date_str} 00:00:00")
            ],
            # Alternative based on date fields if they exist
            [],
            # Simple date string matching (fallback)
            [('start_datetime', '>=', start_date_str), ('start_datetime', '<', next_date_str)]
        ]
        
        # Add shift_status filter if provided
        domains = []
        if shift_status_filter and 'x_studio_shift_status' in available_fields:
            logger.info(f"Filtering planning slots by x_studio_shift_status: {shift_status_filter}")
            for base_domain in base_domains:
                if base_domain:  # Skip empty domain
                    domain_with_filter = base_domain.copy()
                    domain_with_filter.append(('x_studio_shift_status', '=', shift_status_filter))
                    domains.append(domain_with_filter)
        else:
            domains = base_domains
        
        # Basic fields we want, checking which ones exist
        desired_fields = [
            'id', 'name', 'resource_id', 'start_datetime', 'end_datetime', 
            'allocated_hours', 'state', 'project_id', 'task_id', 'x_studio_shift_status',
            'create_uid', 'x_studio_sub_task_1', 'x_studio_task_activity', 'x_studio_service_category_1'
        ]
        
        # Only request fields that exist
        fields_to_request = [f for f in desired_fields if f in available_fields]
        
        # Try each domain until we get results
        all_slots = []
        success = False
        
        for i, domain in enumerate(domains):
            if not domain:  # Skip empty domain
                continue
                
            try:
                logger.info(f"Trying planning slot domain {i+1}")
                slots = models.execute_kw(
                    odoo_db, uid, odoo_password,
                    'planning.slot', 'search_read',
                    [domain],
                    {'fields': fields_to_request}
                )
                
                if slots:
                    logger.info(f"Found {len(slots)} planning slots with domain {i+1}")
                    all_slots.extend(slots)
                    success = True
                    # Don't break, try all domains to get comprehensive results
            except Exception as e:
                # Just log and continue to next domain
                logger.warning(f"Error with planning slot domain {i+1}: {e}")
        
        # If we didn't get any results, try a more permissive approach
        if not success:
            try:
                logger.info("Trying to get all recent planning slots")
                # Get all slots from recent dates
                one_month_ago = (start_date - timedelta(days=30)).strftime("%Y-%m-%d")
                base_domain = [('start_datetime', '>=', one_month_ago)]
                        
                # Add shift_status filter if provided
                if shift_status_filter and 'x_studio_shift_status' in available_fields:
                    base_domain.append(('x_studio_shift_status', '=', shift_status_filter))
                
                recent_slots = models.execute_kw(
                    odoo_db, uid, odoo_password,
                    'planning.slot', 'search_read',
                    [base_domain],
                    {'fields': fields_to_request}
                )
                
                # Filter by date string to find matching ones
                end_date_str_simple = end_date_str.replace('-', '')  # Also try without dashes
                
                for slot in recent_slots:
                    start = slot.get('start_datetime', '')
                    if end_date_str in start or end_date_str_simple in start.replace('-', ''):
                        all_slots.append(slot)
                
                logger.info(f"Filtered to {len(all_slots)} planning slots for the date range")
                
            except Exception as e:
                logger.error(f"Error with permissive planning slot query: {e}")
        
        # Deduplicate slots by ID
        unique_slots = []
        seen_ids = set()
        for slot in all_slots:
            if slot['id'] not in seen_ids:
                unique_slots.append(slot)
                seen_ids.add(slot['id'])
        
        logger.info(f"Returning {len(unique_slots)} unique planning slots for date range {start_date_str} to {end_date_str}")
        return unique_slots
        
    except Exception as e:
        logger.error(f"Error fetching planning slots: {e}")
        st.error(f"Error fetching planning slots: {e}")
        st.session_state.last_error = str(e)
        return []

def get_timesheet_entries(models, uid, odoo_db, odoo_password, start_date, end_date=None):
    """Get timesheet entries for a date range including creation date for timeliness analysis"""
    try:
        if end_date is None:
            end_date = start_date
            
        # Add one day to end_date to include the entire end date
        query_end_date = end_date + timedelta(days=1)
            
        start_date_str = start_date.strftime("%Y-%m-%d")
        end_date_str = query_end_date.strftime("%Y-%m-%d")
        
        # Domain for date range
        domain = [
            ('date', '>=', start_date_str),
            ('date', '<', end_date_str)
        ]
        
        # Get fields for the model to make sure we only request valid fields
        fields_info = get_model_fields(models, uid, odoo_db, odoo_password, 'account.analytic.line')
        available_fields = list(fields_info.keys())
        
        # Fields we want (if they exist) - including create_date for timeliness analysis
        desired_fields = [
            'id', 'name', 'date', 'unit_amount', 'employee_id', 
            'task_id', 'project_id', 'user_id', 'company_id', 'create_date'
        ]
        
        # Only request fields that exist
        fields_to_request = [f for f in desired_fields if f in available_fields]
        
        # Execute query
        logger.info(f"Querying timesheets with domain: {domain}")
        entries = models.execute_kw(
            odoo_db, uid, odoo_password,
            'account.analytic.line', 'search_read',
            [domain],
            {'fields': fields_to_request}
        )
        
        logger.info(f"Found {len(entries)} timesheet entries")
        return entries
    except Exception as e:
        logger.error(f"Error fetching timesheet entries: {e}")
        st.error(f"Error fetching timesheet entries: {e}")
        st.session_state.last_error = str(e)
        return []

def get_references_data(models, uid, odoo_db, odoo_password):
    """Get reference data (projects, users, employees, etc.) for display"""
    reference_data = {}
    
    try:
        # Get resources (employees/equipment in planning)
        resources = models.execute_kw(
            odoo_db, uid, odoo_password,
            'resource.resource', 'search_read',
            [[]],
            {'fields': ['id', 'name', 'user_id', 'resource_type', 'company_id']}
        )
        reference_data['resources'] = {r['id']: r for r in resources}
        
        # Get projects
        projects = models.execute_kw(
            odoo_db, uid, odoo_password,
            'project.project', 'search_read',
            [[]],
            {'fields': ['id', 'name']}
        )
        reference_data['projects'] = {p['id']: p for p in projects}
        
        # Get users
        users = models.execute_kw(
            odoo_db, uid, odoo_password,
            'res.users', 'search_read',
            [[]],
            {'fields': ['id', 'name']}
        )
        reference_data['users'] = {u['id']: u for u in users}
        
        # Get tasks
        tasks = models.execute_kw(
            odoo_db, uid, odoo_password,
            'project.task', 'search_read',
            [[]],
            {'fields': ['id', 'name']}
        )
        reference_data['tasks'] = {t['id']: t for t in tasks}
        
        return reference_data
    except Exception as e:
        logger.error(f"Error fetching reference data: {e}")
        st.warning(f"Error fetching some reference data: {e}")
        st.session_state.last_error = str(e)
        return reference_data

def load_employee_manager_mapping():
    """Load employee-manager relationships from preloaded employee data"""
    try:
        if st.session_state.employee_data is None:
            # Try loading again if not already loaded
            st.session_state.employee_data = load_employee_data()
            
        if st.session_state.employee_data is None:
            logger.error("Cannot load employee-manager mapping: employee data not available")
            return {}
            
        df = st.session_state.employee_data
        
        # Process each employee row
        mapping = {}
        for _, row in df.iterrows():
            try:
                employee_name = row["Employee Name"]
                manager_name = row["Manager"]
                work_email = row["Work Email"]
                
                if pd.notna(employee_name) and pd.notna(manager_name):
                    # Find manager's email by looking up the manager in the dataframe
                    manager_row = df[df["Employee Name"] == manager_name]
                    if not manager_row.empty and pd.notna(manager_row.iloc[0]["Work Email"]):
                        manager_email = manager_row.iloc[0]["Work Email"]
                    else:
                        logger.warning(f"Could not find email for manager '{manager_name}' of employee '{employee_name}'")
                        continue
                    
                    # Store the mapping with the employee name as the key
                    mapping[employee_name] = {
                        "manager_name": manager_name,
                        "manager_email": manager_email
                    }
            except Exception as e:
                logger.warning(f"Error processing row for employee {row.get('Employee Name', 'Unknown')}: {e}")
                continue
        
        logger.info(f"Loaded {len(mapping)} employee-manager relationships")
        return mapping
    except Exception as e:
        logger.error(f"Error loading employee mapping: {e}")
        return {}

def analyze_timesheet_timeliness(timesheet_entries):
    """Analyze how timely people are at entering their timesheets"""
    timeliness_data = []
    
    for entry in timesheet_entries:
        try:
            # Get work date and creation date
            work_date_str = entry.get('date', '')
            create_date_str = entry.get('create_date', '')
            
            if not work_date_str or not create_date_str:
                continue
                
            # Parse dates
            work_date = datetime.strptime(work_date_str, "%Y-%m-%d").date()
            
            # Handle different datetime formats for create_date
            if isinstance(create_date_str, str):
                try:
                    # Try with timezone info first
                    if '+' in create_date_str or create_date_str.endswith('Z'):
                        # Remove timezone info for parsing
                        clean_create_date = create_date_str.split('+')[0].split('Z')[0]
                        create_datetime = datetime.strptime(clean_create_date, "%Y-%m-%d %H:%M:%S")
                    else:
                        create_datetime = datetime.strptime(create_date_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    # Try parsing just the date part
                    create_datetime = datetime.strptime(create_date_str[:10], "%Y-%m-%d")
                    
                create_date = create_datetime.date()
                
                # Calculate delay in days
                delay_days = (create_date - work_date).days
                
                # Get employee name
                employee_name = "Unknown"
                if 'employee_id' in entry and entry['employee_id'] and isinstance(entry['employee_id'], list) and len(entry['employee_id']) > 1:
                    employee_name = entry['employee_id'][1]
                
                # Categorize timeliness
                if delay_days <= 0:
                    timeliness_category = "Same Day"
                elif delay_days == 1:
                    timeliness_category = "1 Day Late"
                elif delay_days == 2:
                    timeliness_category = "2 Days Late"
                else:
                    timeliness_category = "3+ Days Late"
                
                timeliness_data.append({
                    'work_date': work_date,
                    'create_date': create_date,
                    'delay_days': delay_days,
                    'timeliness_category': timeliness_category,
                    'employee_name': employee_name,
                    'entry_id': entry.get('id'),
                    'hours': entry.get('unit_amount', 0)
                })
                
        except Exception as e:
            logger.warning(f"Error processing timesheet entry for timeliness analysis: {e}")
            continue
    
    return pd.DataFrame(timeliness_data)

def get_dashboard_data(end_date, shift_status):
    """Get report data for dashboard without sending any notifications"""
    uid = st.session_state.odoo_uid
    models = st.session_state.odoo_models
    odoo_db = st.session_state.odoo_db
    odoo_password = st.session_state.odoo_password
    
    # Use first day of month as reference date to show full month data 
    reference_date = end_date.replace(day=1)
    
    if not uid or not models:
        st.error("Not connected to Odoo")
        return pd.DataFrame(), 0, 0
    
    try:
        # Step 1: Get all planning slots for the date range
        planning_slots = get_planning_slots(models, uid, odoo_db, odoo_password, reference_date, end_date, shift_status)
        
        # Post-process to ensure only slots with correct shift status are included 
        if shift_status:
            filtered_slots = []
            for slot in planning_slots:
                slot_shift_status = slot.get('x_studio_shift_status', '')
                if slot_shift_status == shift_status:
                    filtered_slots.append(slot)
            planning_slots = filtered_slots
            logger.info(f"Post-filtered to {len(planning_slots)} slots with x_studio_shift_status={shift_status}")
        
        # Step 2: Get all timesheet entries for the date range
        timesheet_entries = get_timesheet_entries(models, uid, odoo_db, odoo_password, reference_date, end_date)
        
        # Step 3: Get reference data
        ref_data = get_references_data(models, uid, odoo_db, odoo_password)
        
        # Step 4: Create resource+task+project to timesheet entry mapping
        resource_task_to_timesheet = {}
        for entry in timesheet_entries:
            employee_id = None
            task_id = None
            project_id = None
            
            # Get employee ID
            if 'employee_id' in entry and entry['employee_id']:
                if isinstance(entry['employee_id'], list):
                    employee_id = entry['employee_id'][0]
                elif isinstance(entry['employee_id'], int):
                    employee_id = entry['employee_id']
            
            # Get task ID 
            if 'task_id' in entry and entry['task_id']:
                if isinstance(entry['task_id'], list):
                    task_id = entry['task_id'][0]
                elif isinstance(entry['task_id'], int):
                    task_id = entry['task_id']
            
            # Get project ID
            if 'project_id' in entry and entry['project_id']:
                if isinstance(entry['project_id'], list):
                    project_id = entry['project_id'][0]
                elif isinstance(entry['project_id'], int):
                    project_id = entry['project_id']
            
            # Get user ID (who actually created/logged the entry)
            user_id = None
            if 'user_id' in entry and entry['user_id']:
                if isinstance(entry['user_id'], list):
                    user_id = entry['user_id'][0]
                elif isinstance(entry['user_id'], int):
                    user_id = entry['user_id']
            
            if employee_id:
                # Create a unique key combining resource, task, and project
                key = (employee_id, task_id, project_id)
                
                if key in resource_task_to_timesheet:
                    resource_task_to_timesheet[key]['hours'] += entry.get('unit_amount', 0)
                    resource_task_to_timesheet[key]['entries'].append(entry)
                    resource_task_to_timesheet[key]['user_ids'].add(user_id)
                else:
                    resource_task_to_timesheet[key] = {
                        'hours': entry.get('unit_amount', 0),
                        'entries': [entry],
                        'user_ids': {user_id} if user_id else set()
                    }
        
        # Add name-based mapping as a fallback
        designer_name_to_timesheet = {}
        for entry in timesheet_entries:
            employee_name = None
            task_id = None
            project_id = None
            
            # Get employee name
            if 'employee_id' in entry and entry['employee_id'] and isinstance(entry['employee_id'], list) and len(entry['employee_id']) > 1:
                employee_name = entry['employee_id'][1]
            
            # Get task ID 
            if 'task_id' in entry and entry['task_id']:
                if isinstance(entry['task_id'], list):
                    task_id = entry['task_id'][0]
                elif isinstance(entry['task_id'], int):
                    task_id = entry['task_id']
            
            # Get project ID
            if 'project_id' in entry and entry['project_id']:
                if isinstance(entry['project_id'], list):
                    project_id = entry['project_id'][0]
                elif isinstance(entry['project_id'], int):
                    project_id = entry['project_id']
            
            # Get user ID 
            user_id = None
            if 'user_id' in entry and entry['user_id']:
                if isinstance(entry['user_id'], list):
                    user_id = entry['user_id'][0]
                elif isinstance(entry['user_id'], int):
                    user_id = entry['user_id']
            
            if employee_name:
                # Create a unique key combining employee name, task, and project
                key = (employee_name, task_id, project_id)
                
                if key in designer_name_to_timesheet:
                    designer_name_to_timesheet[key]['hours'] += entry.get('unit_amount', 0)
                    designer_name_to_timesheet[key]['entries'].append(entry)
                    designer_name_to_timesheet[key]['user_ids'].add(user_id)
                else:
                    designer_name_to_timesheet[key] = {
                        'hours': entry.get('unit_amount', 0),
                        'entries': [entry],
                        'user_ids': {user_id} if user_id else set()
                    }
        
        # Also create a name-only mapping as a last resort
        designer_name_only_to_timesheet = {}
        for entry in timesheet_entries:
            employee_name = None
            
            # Get employee name
            if 'employee_id' in entry and entry['employee_id'] and isinstance(entry['employee_id'], list) and len(entry['employee_id']) > 1:
                employee_name = entry['employee_id'][1]
            
            # Get user ID
            user_id = None
            if 'user_id' in entry and entry['user_id']:
                if isinstance(entry['user_id'], list):
                    user_id = entry['user_id'][0]
                elif isinstance(entry['user_id'], int):
                    user_id = entry['user_id']
            
            if employee_name:
                if employee_name in designer_name_only_to_timesheet:
                    designer_name_only_to_timesheet[employee_name]['hours'] += entry.get('unit_amount', 0)
                    designer_name_only_to_timesheet[employee_name]['entries'].append(entry)
                    designer_name_only_to_timesheet[employee_name]['user_ids'].add(user_id)
                else:
                    designer_name_only_to_timesheet[employee_name] = {
                        'hours': entry.get('unit_amount', 0),
                        'entries': [entry],
                        'user_ids': {user_id} if user_id else set()
                    }
        
        # Generate report data
        report_data = []
        
        for slot in planning_slots:
            # Get resource info
            resource_id = None
            if 'resource_id' in slot and slot['resource_id'] and isinstance(slot['resource_id'], list):
                resource_id = slot['resource_id'][0]
                resource_name = slot['resource_id'][1] if len(slot['resource_id']) > 1 else "Unknown"
            else:
                resource_name = "Unknown"
            
            # Get task ID
            task_id = None
            if 'task_id' in slot and slot['task_id'] and isinstance(slot['task_id'], list):
                task_id = slot['task_id'][0]
                task_name = "Unknown"
                if task_id in ref_data.get('tasks', {}):
                    task_name = ref_data['tasks'][task_id].get('name', 'Unknown')
            else:
                task_name = "Unknown"
            
            # Get project ID
            project_id = None
            if 'project_id' in slot and slot['project_id'] and isinstance(slot['project_id'], list):
                project_id = slot['project_id'][0]
                project_name = "Unknown"
                if project_id in ref_data.get('projects', {}):
                    project_name = ref_data['projects'][project_id].get('name', 'Unknown')
            else:
                project_name = "Unknown"
            
            # Check if this resource/employee has logged time for this specific task/project
            has_timesheet = False
            hours_logged = 0.0
            
            # First check: exact match by resource_id + task_id + project_id
            key = (resource_id, task_id, project_id)
            if key in resource_task_to_timesheet:
                hours_logged = resource_task_to_timesheet[key]['hours']
                
                # Get the user_id associated with the resource (if available)
                resource_user_id = None
                if resource_id in ref_data.get('resources', {}) and ref_data['resources'][resource_id].get('user_id'):
                    resource_details = ref_data['resources'][resource_id]
                    if isinstance(resource_details['user_id'], list) and len(resource_details['user_id']) > 0:
                        resource_user_id = resource_details['user_id'][0]
                    elif isinstance(resource_details['user_id'], int):
                        resource_user_id = resource_details['user_id']
                
                # Only consider it a valid timesheet if hours logged are greater than 0
                user_ids = resource_task_to_timesheet[key]['user_ids']
                has_timesheet = (hours_logged > 0) and (resource_user_id in user_ids if resource_user_id else False)
            
            # Second check: try matching by name + task_id + project_id
            if not has_timesheet and resource_name != "Unknown":
                name_key = (resource_name, task_id, project_id)
                if name_key in designer_name_to_timesheet:
                    hours_logged = designer_name_to_timesheet[name_key]['hours']
                    has_timesheet = hours_logged > 0
            
            # Last resort: check if designer has ANY timesheet for the day
            if not has_timesheet and resource_name != "Unknown":
                if resource_name in designer_name_only_to_timesheet:
                    hours_logged = designer_name_only_to_timesheet[resource_name]['hours']
                    has_timesheet = hours_logged > 0
            
            # Get other slot info for display
            slot_name = slot.get('name', 'Unnamed Slot')
            
            # Convert boolean values to strings
            if isinstance(slot_name, bool):
                slot_name = str(slot_name)
            
            # Get shift status for display
            shift_status_value = slot.get('x_studio_shift_status', 'Unknown')
            
            # Get client success member (create_uid)
            client_success_name = "Unknown"
            if 'create_uid' in slot and slot['create_uid'] and isinstance(slot['create_uid'], list):
                create_uid = slot['create_uid'][0]
                if create_uid in ref_data.get('users', {}):
                    client_success_name = ref_data['users'][create_uid].get('name', 'Unknown')
            
            # Format start and end times for display
            start_datetime = slot.get('start_datetime', '')
            end_datetime = slot.get('end_datetime', '')
            
            start_time = "Unknown"
            end_time = "Unknown"
            
            if start_datetime and isinstance(start_datetime, str):
                try:
                    # Convert string to datetime
                    start_dt = datetime.strptime(start_datetime, "%Y-%m-%d %H:%M:%S")
                    start_time = start_dt.strftime("%H:%M")
                except:
                    start_time = start_datetime
            
            if end_datetime and isinstance(end_datetime, str):
                try:
                    # Convert string to datetime
                    end_dt = datetime.strptime(end_datetime, "%Y-%m-%d %H:%M:%S")
                    end_time = end_dt.strftime("%H:%M")
                except:
                    end_time = end_datetime
            
            # Get time allocation
            allocated_hours = slot.get('allocated_hours', 0.0)
            
            # Extract task date from slot data
            task_date = None
            if start_datetime and isinstance(start_datetime, str):
                try:
                    # Convert string to datetime
                    task_date = datetime.strptime(start_datetime, "%Y-%m-%d %H:%M:%S").date()
                except:
                    # If parsing fails, use the selected date
                    task_date = end_date
            else:
                # Fallback if no valid start_datetime
                task_date = end_date
                
            # Calculate days since task date for urgency
            reference_point = end_date
            days_since_task = (reference_point - task_date).days
            
            # Only include slots with no timesheet since we're reporting missing timesheets
            if not has_timesheet:
                task_data = {
                    'Date': task_date.strftime("%Y-%m-%d"),
                    'Designer': str(resource_name),
                    'Project': str(project_name),
                    'Client Success Member': str(client_success_name),
                    'Task': str(task_name),
                    'Start Time': str(start_time),
                    'End Time': str(end_time),
                    'Allocated Hours': float(allocated_hours),
                    'Days Overdue': int(days_since_task),
                    'Urgency': 'High' if days_since_task >= 2 else ('Medium' if days_since_task == 1 else 'Low')
                }
                
                report_data.append(task_data)
        
        # Convert to DataFrame
        if report_data:
            # Ensure all values are properly converted to appropriate types
            for item in report_data:
                # Convert any boolean values to strings
                for key, value in item.items():
                    if isinstance(value, bool):
                        item[key] = str(value)
                    # Handle other problematic types if needed
                    elif value is None:
                        item[key] = ""
            
            df = pd.DataFrame(report_data)
            return df, len(report_data), len(timesheet_entries)
        else:
            # Return empty DataFrame with columns
            empty_df = pd.DataFrame(columns=[
                'Date', 'Designer', 'Project', 'Client Success Member', 'Task', 
                'Start Time', 'End Time', 'Allocated Hours', 'Days Overdue', 'Urgency'
            ])
            return empty_df, 0, len(timesheet_entries)
    except Exception as e:
        logger.error(f"Error generating report: {e}")
        st.error(f"Error generating report: {e}")
        return pd.DataFrame(), 0, len(timesheet_entries) if 'timesheet_entries' in locals() else 0

def get_historical_compliance_data(start_date, end_date, shift_status=None):
    """Get compliance data for each day in the date range"""
    data = []
    
    # To avoid too many API calls, calculate for weekly points
    # if the date range is more than 14 days
    date_diff = (end_date - start_date).days
    
    if date_diff > 14:
        # Use weekly intervals
        interval = 7
    else:
        # Use daily intervals
        interval = 1
        
    current_date = start_date
    
    while current_date <= end_date:
        try:
            # Get missing entries for this date - using the safe function
            df, missing_count, timesheet_count = get_dashboard_data(current_date, shift_status)
            
            # Calculate compliance rate
            total_entries = missing_count + timesheet_count
            compliance_rate = (timesheet_count / total_entries * 100) if total_entries > 0 else 100
            
            # Add to data
            data.append({
                "Date": current_date,
                "ComplianceRate": compliance_rate,
                "MissingCount": missing_count,
                "TimesheetCount": timesheet_count,
                "TotalEntries": total_entries
            })
            
        except Exception as e:
            logger.error(f"Error getting data for {current_date}: {e}")
            # Add empty data point to maintain timeline
            data.append({
                "Date": current_date,
                "ComplianceRate": None,
                "MissingCount": 0,
                "TimesheetCount": 0,
                "TotalEntries": 0
            })
        
        # Move to next interval
        current_date += timedelta(days=interval)
    
    # Convert to DataFrame
    return pd.DataFrame(data)

def render_summary_metrics(df, missing_count, timesheet_count):
    """Render summary metrics section"""
    st.header("Summary Metrics")
    
    # Calculate overall compliance rate
    total_entries = missing_count + timesheet_count
    compliance_rate = (timesheet_count / total_entries * 100) if total_entries > 0 else 100
    
    # Display key metrics
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric(
            "Compliance Rate", 
            f"{compliance_rate:.1f}%",
            help="Percentage of planned hours that have been logged"
        )
    
    with col2:
        st.metric(
            "Missing Entries", 
            f"{missing_count}",
            help="Number of planning slots without timesheet entries"
        )
    
    with col3:
        st.metric(
            "Logged Entries", 
            f"{timesheet_count}",
            help="Number of timesheet entries found"
        )
    
    with col4:
        # Calculate total hours missing
        total_hours_missing = df["Allocated Hours"].sum() if not df.empty else 0
        st.metric(
            "Total Hours Missing", 
            f"{total_hours_missing:.1f}h",
            help="Total hours allocated in planning but not logged in timesheets"
        )
    
    # Urgency breakdown
    if not df.empty and "Urgency" in df.columns:
        st.subheader("Urgency Breakdown")
        
        # Calculate counts by urgency
        urgency_counts = df["Urgency"].value_counts().reset_index()
        urgency_counts.columns = ["Urgency", "Count"]
        
        # Define urgency order and colors
        urgency_order = {"High": 0, "Medium": 1, "Low": 2}
        urgency_colors = {"High": "#FF4B4B", "Medium": "#FFA421", "Low": "#21AFF5"}
        
        # Add sorting column and sort
        urgency_counts["Order"] = urgency_counts["Urgency"].map(urgency_order)
        urgency_counts = urgency_counts.sort_values("Order").drop("Order", axis=1)
        
        # Create horizontal bar chart with Altair
        chart = alt.Chart(urgency_counts).mark_bar().encode(
            x=alt.X("Count:Q", title="Count"),
            y=alt.Y("Urgency:N", sort=list(urgency_order.keys()), title=""),
            color=alt.Color("Urgency:N", scale=alt.Scale(
                domain=list(urgency_colors.keys()),
                range=list(urgency_colors.values())
            )),
            tooltip=["Urgency", "Count"]
        ).properties(height=200)
        
        st.altair_chart(chart, use_container_width=True)

def render_timesheet_timeliness_analysis(start_date, end_date):
    """Render analysis of how timely people are at entering timesheets"""
    st.header("⏰ Timesheet Entry Timeliness Analysis")
    st.markdown("*Analyzing how quickly people enter their timesheet after completing work*")
    
    # Get timesheet data for analysis
    uid = st.session_state.odoo_uid
    models = st.session_state.odoo_models
    odoo_db = st.session_state.odoo_db
    odoo_password = st.session_state.odoo_password
    
    if not uid or not models:
        st.error("Not connected to Odoo")
        return
    
    with st.spinner("Analyzing timesheet entry patterns..."):
        # Get timesheet entries for the period
        timesheet_entries = get_timesheet_entries(models, uid, odoo_db, odoo_password, start_date, end_date)
        
        if not timesheet_entries:
            st.info("No timesheet entries found for the selected date range.")
            return
        
        # Analyze timeliness
        timeliness_df = analyze_timesheet_timeliness(timesheet_entries)
        
        if timeliness_df.empty:
            st.warning("Could not analyze timesheet timeliness due to missing date information.")
            return
    
    # Display overall timeliness metrics
    col1, col2, col3, col4 = st.columns(4)
    
    total_entries = len(timeliness_df)
    same_day_entries = len(timeliness_df[timeliness_df['timeliness_category'] == 'Same Day'])
    late_entries = len(timeliness_df[timeliness_df['delay_days'] > 0])
    avg_delay = timeliness_df['delay_days'].mean()
    
    with col1:
        same_day_percentage = (same_day_entries / total_entries * 100) if total_entries > 0 else 0
        st.metric(
            "Same Day Entry Rate", 
            f"{same_day_percentage:.1f}%",
            help="Percentage of timesheets entered on the same day as work was performed"
        )
    
    with col2:
        st.metric(
            "Late Entries", 
            f"{late_entries}",
            help="Number of timesheet entries submitted after the work date"
        )
    
    with col3:
        st.metric(
            "Average Delay", 
            f"{avg_delay:.1f} days",
            help="Average number of days between work date and timesheet entry"
        )
    
    with col4:
        # Calculate very late entries (3+ days)
        very_late_entries = len(timeliness_df[timeliness_df['delay_days'] >= 3])
        st.metric(
            "Very Late Entries (3+ days)", 
            f"{very_late_entries}",
            help="Entries submitted 3 or more days after work was performed"
        )
    
    # Timeliness distribution chart
    st.subheader("Timesheet Entry Timeliness Distribution")
    
    # Calculate distribution
    timeliness_counts = timeliness_df['timeliness_category'].value_counts().reset_index()
    timeliness_counts.columns = ['Timeliness Category', 'Count']
    
    # Define order and colors for consistency
    category_order = ['Same Day', '1 Day Late', '2 Days Late', '3+ Days Late']
    category_colors = {
        'Same Day': '#00CC96',
        '1 Day Late': '#FFA15A', 
        '2 Days Late': '#FF6692',
        '3+ Days Late': '#EF553B'
    }
    
    # Create the chart
    chart = alt.Chart(timeliness_counts).mark_bar().encode(
        x=alt.X('Count:Q', title='Number of Entries'),
        y=alt.Y('Timeliness Category:N', 
                sort=category_order,
                title=''),
        color=alt.Color('Timeliness Category:N',
                       scale=alt.Scale(
                           domain=list(category_colors.keys()),
                           range=list(category_colors.values())
                       ),
                       legend=None),
        tooltip=['Timeliness Category', 'Count']
    ).properties(height=300)
    
    st.altair_chart(chart, use_container_width=True)
    
    # Daily trend analysis
    st.subheader("Daily Timeliness Trends")
    
    # Group by work date and calculate daily timeliness metrics
    daily_timeliness = timeliness_df.groupby('work_date').agg({
        'delay_days': ['count', 'mean'],
        'timeliness_category': lambda x: (x == 'Same Day').sum()
    }).reset_index()
    
    # Flatten column names
    daily_timeliness.columns = ['work_date', 'total_entries', 'avg_delay', 'same_day_count']
    daily_timeliness['same_day_rate'] = (daily_timeliness['same_day_count'] / daily_timeliness['total_entries'] * 100)
    
    # Create trend chart
    trend_chart = alt.Chart(daily_timeliness).mark_line(point=True).encode(
        x=alt.X('work_date:T', title='Work Date'),
        y=alt.Y('same_day_rate:Q', 
                scale=alt.Scale(domain=[0, 100]),
                title='Same Day Entry Rate (%)'),
        tooltip=['work_date:T', 'same_day_rate:Q', 'total_entries:Q', 'avg_delay:Q']
    ).properties(height=300)
    
    st.altair_chart(trend_chart, use_container_width=True)
    
    # Employee timeliness rankings
    st.subheader("Employee Timeliness Performance")
    
    employee_timeliness = timeliness_df.groupby('employee_name').agg({
        'delay_days': ['count', 'mean'],
        'timeliness_category': lambda x: (x == 'Same Day').sum(),
        'hours': 'sum'
    }).reset_index()
    
    # Flatten column names
    employee_timeliness.columns = ['employee_name', 'total_entries', 'avg_delay', 'same_day_count', 'total_hours']
    employee_timeliness['same_day_rate'] = (employee_timeliness['same_day_count'] / employee_timeliness['total_entries'] * 100)
    
    # Filter to employees with at least 3 entries for meaningful analysis
    employee_timeliness = employee_timeliness[employee_timeliness['total_entries'] >= 3]
    
    # Sort by same day rate
    employee_timeliness = employee_timeliness.sort_values('same_day_rate', ascending=False)
    
    if not employee_timeliness.empty:
        # Display top and bottom performers
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("**🏆 Top Performers (Same Day Entry Rate)**")
            top_performers = employee_timeliness.head(10)
            st.dataframe(
                top_performers[['employee_name', 'same_day_rate', 'total_entries', 'avg_delay']].round(1),
                use_container_width=True
            )
        
        with col2:
            st.markdown("**⚠️ Needs Improvement (Lowest Same Day Rates)**")
            bottom_performers = employee_timeliness.tail(10)
            st.dataframe(
                bottom_performers[['employee_name', 'same_day_rate', 'total_entries', 'avg_delay']].round(1),
                use_container_width=True
            )
        
        # Create employee performance chart (top 15 by entry count)
        top_by_volume = employee_timeliness.nlargest(15, 'total_entries')
        
        emp_chart = alt.Chart(top_by_volume).mark_bar().encode(
            x=alt.X('same_day_rate:Q', title='Same Day Entry Rate (%)'),
            y=alt.Y('employee_name:N', sort='-x', title=''),
            color=alt.Color('same_day_rate:Q', 
                           scale=alt.Scale(scheme='redyellowgreen'),
                           legend=None),
            tooltip=['employee_name', 'same_day_rate:Q', 'total_entries:Q', 'avg_delay:Q']
        ).properties(height=400)
        
        st.altair_chart(emp_chart, use_container_width=True)
    else:
        st.info("Not enough data for meaningful employee analysis (need at least 3 entries per employee).")

def render_compliance_trend(historical_data):
    """Render compliance trend chart"""
    st.header("Compliance Trend")
    
    if historical_data.empty:
        st.info("No historical data available for the selected date range.")
        return
    
    # Create line chart for compliance trend
    chart = alt.Chart(historical_data).mark_line(point=True).encode(
        x=alt.X("Date:T", title="Date"),
        y=alt.Y("ComplianceRate:Q", scale=alt.Scale(domain=[0, 100]), title="Compliance Rate (%)"),
        tooltip=["Date:T", "ComplianceRate:Q", "MissingCount:Q", "TimesheetCount:Q"]
    ).properties(height=300)
    
    st.altair_chart(chart, use_container_width=True)

def render_team_metrics(df):
    """Render team metrics section"""
    st.header("Team Compliance Metrics")
    
    if df.empty:
        st.info("No missing timesheet entries found for the selected filters.")
        return
    
    # Load employee-manager mapping
    employee_manager_mapping = load_employee_manager_mapping()
    
    if not employee_manager_mapping:
        st.warning("Employee-manager mapping not available. Make sure the employee data CSV is loaded.")
        return
    
    # Add manager information to the dataframe
    df_with_managers = df.copy()
    df_with_managers["Manager"] = df_with_managers["Designer"].map(
        lambda d: employee_manager_mapping.get(d, {}).get("manager_name", "Unknown")
    )
    
    # Group by manager
    manager_metrics = df_with_managers.groupby("Manager").agg(
        MissingEntries=("Designer", "count"),
        UniqueDesigners=("Designer", lambda x: len(set(x))),
        TotalHoursMissing=("Allocated Hours", "sum")
    ).reset_index()
    
    # Sort by missing entries
    manager_metrics = manager_metrics.sort_values("MissingEntries", ascending=False)
    
    # Display table
    st.dataframe(manager_metrics, use_container_width=True)
    
    # Create bar chart for managers
    chart = alt.Chart(manager_metrics).mark_bar().encode(
        x=alt.X("MissingEntries:Q", title="Missing Entries"),
        y=alt.Y("Manager:N", sort="-x", title=""),
        color=alt.Color("TotalHoursMissing:Q", scale=alt.Scale(scheme="reds")),
        tooltip=["Manager", "MissingEntries", "UniqueDesigners", "TotalHoursMissing"]
    ).properties(height=300)
    
    st.altair_chart(chart, use_container_width=True)
    
    # Manager drill-down
    st.subheader("Team Drill-Down")
    selected_manager = st.selectbox(
        "Select Manager to View Team", 
        ["All Managers"] + sorted(manager_metrics["Manager"].tolist())
    )
    
    if selected_manager != "All Managers":
        # Filter to selected manager's team
        team_df = df_with_managers[df_with_managers["Manager"] == selected_manager]
        
        # Group by designer
        designer_metrics = team_df.groupby("Designer").agg(
            MissingEntries=("Task", "count"),
            TotalHoursMissing=("Allocated Hours", "sum"),
            HighUrgencyTasks=("Urgency", lambda x: (x == "High").sum()),
            MediumUrgencyTasks=("Urgency", lambda x: (x == "Medium").sum()),
            LowUrgencyTasks=("Urgency", lambda x: (x == "Low").sum())
        ).reset_index()
        
        # Sort by missing entries
        designer_metrics = designer_metrics.sort_values("MissingEntries", ascending=False)
        
        # Display team table
        st.subheader(f"Designers in {selected_manager}'s Team")
        st.dataframe(designer_metrics, use_container_width=True)
        
        # Display individual missing entries
        if not team_df.empty:
            st.subheader(f"Missing Entries for {selected_manager}'s Team")
            st.dataframe(team_df, use_container_width=True)

def render_designer_metrics(df):
    """Render designer metrics section"""
    st.header("Designer Compliance Metrics")
    
    if df.empty:
        st.info("No missing timesheet entries found for the selected filters.")
        return
    
    # Group by designer
    designer_metrics = df.groupby("Designer").agg(
        MissingEntries=("Task", "count"),
        TotalHoursMissing=("Allocated Hours", "sum"),
        HighUrgencyTasks=("Urgency", lambda x: (x == "High").sum()),
        MediumUrgencyTasks=("Urgency", lambda x: (x == "Medium").sum()),
        LowUrgencyTasks=("Urgency", lambda x: (x == "Low").sum()),
        MaxDaysOverdue=("Days Overdue", "max")
    ).reset_index()
    
    # Sort by missing entries
    designer_metrics = designer_metrics.sort_values("MissingEntries", ascending=False)
    
    # Calculate a risk score (custom formula)
    designer_metrics["RiskScore"] = (
        designer_metrics["HighUrgencyTasks"] * 10 + 
        designer_metrics["MediumUrgencyTasks"] * 3 + 
        designer_metrics["TotalHoursMissing"] * 0.5
    )
    
    # Take top 10 designers by missing entries
    top_designers = designer_metrics.head(10)
    
    # Create bar chart for top designers
    chart = alt.Chart(top_designers).mark_bar().encode(
        x=alt.X("MissingEntries:Q", title="Missing Entries"),
        y=alt.Y("Designer:N", sort="-x", title=""),
        color=alt.Color("RiskScore:Q", scale=alt.Scale(scheme="reds")),
        tooltip=["Designer", "MissingEntries", "TotalHoursMissing", "HighUrgencyTasks", "MediumUrgencyTasks", "MaxDaysOverdue"]
    ).properties(height=350)
    
    st.altair_chart(chart, use_container_width=True)
    
    # Designer drill-down
    st.subheader("Designer Drill-Down")
    selected_designer = st.selectbox(
        "Select Designer to View Details", 
        ["All Designers"] + sorted(designer_metrics["Designer"].tolist())
    )
    
    if selected_designer != "All Designers":
        # Filter to selected designer
        designer_df = df[df["Designer"] == selected_designer]
        
        # Display designer metrics
        designer_row = designer_metrics[designer_metrics["Designer"] == selected_designer].iloc[0]
        
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Missing Entries", int(designer_row["MissingEntries"]))
        with col2:
            st.metric("Hours Missing", f"{designer_row['TotalHoursMissing']:.1f}")
        with col3:
            st.metric("High Urgency Tasks", int(designer_row["HighUrgencyTasks"]))
        with col4:
            st.metric("Max Days Overdue", int(designer_row["MaxDaysOverdue"]))
        
        # Display individual missing entries
        st.subheader(f"Missing Entries for {selected_designer}")
        st.dataframe(designer_df, use_container_width=True)

def render_project_metrics(df):
    """Render project metrics section"""
    st.header("Project Compliance Metrics")
    
    if df.empty:
        st.info("No missing timesheet entries found for the selected filters.")
        return
    
    # Group by project
    project_metrics = df.groupby("Project").agg(
        MissingEntries=("Task", "count"),
        UniqueDesigners=("Designer", lambda x: len(set(x))),
        TotalHoursMissing=("Allocated Hours", "sum"),
        HighUrgencyTasks=("Urgency", lambda x: (x == "High").sum())
    ).reset_index()
    
    # Sort by missing entries
    project_metrics = project_metrics.sort_values("MissingEntries", ascending=False)
    
    # Take top 10 projects
    top_projects = project_metrics.head(10)
    
    # Create bar chart for top projects
    chart = alt.Chart(top_projects).mark_bar().encode(
        x=alt.X("MissingEntries:Q", title="Missing Entries"),
        y=alt.Y("Project:N", sort="-x", title=""),
        color=alt.Color("TotalHoursMissing:Q", scale=alt.Scale(scheme="blues")),
        tooltip=["Project", "MissingEntries", "UniqueDesigners", "TotalHoursMissing", "HighUrgencyTasks"]
    ).properties(height=350)
    
    st.altair_chart(chart, use_container_width=True)
    
    # Project drill-down
    st.subheader("Project Drill-Down")
    selected_project = st.selectbox(
        "Select Project to View Details", 
        ["All Projects"] + sorted(project_metrics["Project"].tolist())
    )
    
    if selected_project != "All Projects":
        # Filter to selected project
        project_df = df[df["Project"] == selected_project]
        
        # Group by designer for this project
        project_designers = project_df.groupby("Designer").agg(
            MissingEntries=("Task", "count"),
            TotalHoursMissing=("Allocated Hours", "sum")
        ).reset_index().sort_values("MissingEntries", ascending=False)
        
        # Display project designers table
        st.subheader(f"Designers with Missing Entries on {selected_project}")
        st.dataframe(project_designers, use_container_width=True)
        
        # Display individual missing entries
        st.subheader(f"Missing Entries for {selected_project}")
        st.dataframe(project_df, use_container_width=True)

def main():
    st.set_page_config(
        page_title="Timesheet Compliance Dashboard",
        page_icon="📊",
        layout="wide",
    )
    
    # Add logo and title
    st.title("📊 Timesheet Compliance Dashboard")
    st.markdown("### View team compliance metrics and track missing timesheets")
    
    # Auto-connect to Odoo with permanent credentials
    if not st.session_state.odoo_uid or not st.session_state.odoo_models:
        with st.spinner("Connecting to Odoo..."):
            uid, models = authenticate_odoo(
                st.session_state.odoo_url,
                st.session_state.odoo_db, 
                st.session_state.odoo_username,
                st.session_state.odoo_password
            )
            
            if uid and models:
                st.session_state.odoo_uid = uid
                st.session_state.odoo_models = models

    # Sidebar content
    with st.sidebar:
        st.header("Dashboard Controls")
        
        # Date selector
        st.subheader("Date Range")
        
        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input(
                "Start Date", 
                date.today().replace(day=1),  # First day of current month
                help="Start date for dashboard data"
            )
        with col2:
            end_date = st.date_input(
                "End Date", 
                date.today(),
                help="End date for dashboard data"
            )
        
        # Add filter for shift status
        st.subheader("Shift Status Filter")
        shift_status_filter = st.selectbox(
            "Show slots with shift status:",
            ["All", "Planned (Confirmed)", "Forecasted (Unconfirmed)"],
            index=1  # Default to "Planned (Confirmed)"
        )
        
        # Convert shift status selection to filter value
        if shift_status_filter == "All":
            shift_status = None
        elif shift_status_filter == "Planned (Confirmed)":
            shift_status = "Planned"
        else:
            shift_status = "Forecasted"
        
        # Connection status
        st.header("Odoo Connection")
        if st.session_state.odoo_uid and st.session_state.odoo_models:
            st.success(f"✅ Connected as {st.session_state.odoo_username}")
        else:
            st.error("❌ Not connected to Odoo")
            
            if st.button("Retry Connection"):
                st.rerun()
            
    # Main area content
    # Check if connected to Odoo
    if not st.session_state.odoo_uid or not st.session_state.odoo_models:
        st.error("Failed to connect to Odoo. Please check connection settings and try again.")
        return
    
    # Ensure employee data is loaded
    if st.session_state.employee_data is None:
        st.session_state.employee_data = load_employee_data()
        if st.session_state.employee_data is None:
            st.warning("Employee data could not be loaded. Some features may not work correctly.")
    
    # Fetch data for dashboard
    with st.spinner("Loading dashboard data..."):
        missing_df, missing_count, timesheet_count = get_dashboard_data(end_date, shift_status)
    
    # Render dashboard sections
    render_summary_metrics(missing_df, missing_count, timesheet_count)
    
    # Add the new timesheet timeliness analysis
    render_timesheet_timeliness_analysis(start_date, end_date)
    
    render_team_metrics(missing_df)
    render_designer_metrics(missing_df) 
    render_project_metrics(missing_df)
    
    # Historical compliance trend
    if st.checkbox("Show Historical Compliance Trend (May take a while to load)"):
        with st.spinner("Generating historical data..."):
            historical_data = get_historical_compliance_data(start_date, end_date, shift_status)
            render_compliance_trend(historical_data)
    
    # Footer
    st.markdown("---")
    st.markdown(
        "📊 **Timesheet Compliance Dashboard** | Data from Odoo Planning Module | "
        "Last updated: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

if __name__ == "__main__":
    main()