# Railway Deployment Guide

This guide will walk you through deploying the Tracker Web App to Railway.

## Prerequisites

- A GitHub account
- Git installed on your computer
- Railway account (free tier available)

## Step 1: Prepare Your Repository

### 1.1 Initialize Git (if not already done)

Open PowerShell or Command Prompt in the `tracker_webapp` directory:

```bash
cd "C:\Users\SidharthSunil\OneDrive - FTC Solar\Desktop\Lewis_tracker_boundaries_corrected\tracker_webapp"
git init
```

### 1.2 Add All Files

```bash
git add .
```

### 1.3 Create Initial Commit

```bash
git commit -m "Initial commit - Railway deployment ready"
```

## Step 2: Create GitHub Repository

### 2.1 Create New Repository on GitHub

1. Go to [github.com](https://github.com) and sign in
2. Click the **+** icon in the top right → **New repository**
3. Repository name: `tracker-webapp` (or any name you prefer)
4. Description: "Interactive Tracker Viewer Web App"
5. Choose **Public** or **Private**
6. **DO NOT** initialize with README, .gitignore, or license (you already have these)
7. Click **Create repository**

### 2.2 Connect Local Repository to GitHub

GitHub will show you commands. Use these (replace `YOUR_USERNAME` with your GitHub username):

```bash
git remote add origin https://github.com/YOUR_USERNAME/tracker-webapp.git
git branch -M main
git push -u origin main
```

You'll be prompted for your GitHub credentials.

## Step 3: Set Up Railway Account

### 3.1 Sign Up for Railway

1. Go to [railway.app](https://railway.app)
2. Click **Start a New Project**
3. Sign up with your **GitHub account** (recommended for easy deployment)

## Step 4: Deploy to Railway

### 4.1 Create New Project

1. In Railway dashboard, click **New Project**
2. Select **Deploy from GitHub repo**
3. Authorize Railway to access your GitHub if prompted
4. Select your `tracker-webapp` repository
5. Railway will automatically start deploying

### 4.2 Configure Root Directory

**IMPORTANT:** Railway needs to know where your app is located.

1. In your Railway project, go to **Settings**
2. Scroll down to **Source**
3. Set **Root Directory** to: `tracker_webapp`
4. Click **Save**

### 4.3 Configure Environment Variables (Optional)

If you need to customize paths, go to **Variables** tab and add:

- `FLASK_DEBUG=False` (for production)
- `PORT` is automatically set by Railway (don't override)

## Step 5: Get Your App URL

1. After deployment completes, go to **Settings** → **Domains**
2. Railway provides a default domain like: `your-app-name.up.railway.app`
3. You can also add a custom domain if you have one

## Step 6: Important Notes

### File Paths

Your app currently references files outside `tracker_webapp`:
- `Output_Lewis/` (parent directory)
- `Lewistifs/` (parent directory)

**Options:**

**Option A: Move Data into tracker_webapp (Recommended)**
1. Copy `Output_Lewis/` folder into `tracker_webapp/`
2. Copy `Lewistifs/` folder into `tracker_webapp/`
3. Update paths in `app.py` if needed (they should auto-detect)

**Option B: Use Railway Volumes**
1. In Railway project, click **New** → **Volume**
2. Mount volumes for persistent storage
3. Update environment variables to point to volume paths

**Option C: Use Cloud Storage**
- Upload large files to AWS S3, Google Cloud Storage, or similar
- Update code to fetch from cloud storage

### First Deployment

- Railway will automatically install dependencies from `requirements.txt`
- The build process may take 5-10 minutes
- Watch the **Deployments** tab for build logs
- If deployment fails, check logs for errors

## Step 7: Monitor Your Deployment

1. **Deployments Tab**: See build and deployment status
2. **Metrics Tab**: Monitor CPU, memory, and network usage
3. **Logs Tab**: View application logs in real-time

## Troubleshooting

### Build Fails

- Check that `requirements.txt` has all dependencies
- Verify `Procfile` exists and is correct
- Check build logs for specific errors

### App Doesn't Start

- Check logs in Railway dashboard
- Verify PORT environment variable is set (Railway sets this automatically)
- Ensure `gunicorn` is in `requirements.txt`

### Files Not Found

- Verify file paths in `app.py` are correct
- Check that `layout_data/` folder is in the repository
- Ensure large files are committed (GitHub has 100MB file limit)

### Large Files Issue

If your repository is too large:
- Use Git LFS for large files
- Or use cloud storage for images/TIFFs
- Or use Railway Volumes

## Updating Your App

After making changes:

```bash
git add .
git commit -m "Your commit message"
git push origin main
```

Railway will automatically detect the push and redeploy your app.

## Cost

- Railway offers a **free tier** with $5 credit/month
- After free tier, pricing is pay-as-you-go
- Check Railway pricing page for current rates

## Support

- Railway Docs: https://docs.railway.app
- Railway Discord: https://discord.gg/railway

